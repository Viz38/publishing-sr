import asyncio
import aiohttp
from camoufox.async_api import AsyncCamoufox
import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials
from bs4 import BeautifulSoup
import logging
import re
import json
import os
import psutil
from datetime import datetime
from typing import Dict, List, Set, Tuple, Optional, Union, Any
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

from sr_common.config import settings
from sr_common.clients import RateLimiter, GoogleSheetsClient
from sr_common.utils import call_gemini_api, call_tracxn_api, clean_html, extract_descriptions, is_parked_domain, get_dynamic_max_workers, SystemHealthMonitor

_DYNAMIC_WORKERS = get_dynamic_max_workers()

# Configuration
CONFIG = {
    "SHEET_ID": settings.TYPEA_SHEET_ID,
    "MASTER_SHEET_ID": settings.MASTER_SHEET_ID,
    "PROMPTS_SHEET_ID": settings.PROMPTS_SHEET_ID,
    "FEED_OWNER_SHEET_ID": settings.FEED_OWNER_SHEET_ID,
    "FEED_DEF_SHEET_ID_1": settings.FEED_DEF_SHEET_ID_1,
    "FEED_DEF_SHEET_ID_2": settings.FEED_DEF_SHEET_ID_2,
    "EXTRACTING_SHEET_NAME": "DB",
    "CREDENTIALS_FILE": os.path.join(os.path.dirname(os.path.abspath(__file__)), "TypeA.json"),
    "MAX_WORKERS": _DYNAMIC_WORKERS,
    "MAX_CONCURRENT_BROWSERS": _DYNAMIC_WORKERS,
    "GEMINI_API_URL": settings.GEMINI_API_URL,
    "GEMINI_API_KEY": settings.TYPEA_GEMINI_API_KEY,
    "MAX_PROMPT_SIZE": settings.MAX_PROMPT_SIZE,
    "BATCH_SIZE": settings.BATCH_SIZE,
    "REQUEST_TIMEOUT": settings.REQUEST_TIMEOUT,
    "MAX_RETRIES": settings.MAX_RETRIES,
    "RETRY_DELAY": settings.RETRY_DELAY
}

HEADERS = {
    "accessToken": settings.TYPEA_TRACXN_ACCESS_TOKEN,
    "Content-Type": "application/json",
    "X-Request-Source": 'Type-A-Publishing'
}

MAIN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
}

# Configure logging
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Logs')
os.makedirs(LOGS_DIR, exist_ok=True)

def setup_logger(name, log_file, level=logging.INFO):
    handler = logging.FileHandler(log_file, mode='a')
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    # Avoid propagation to root if we want strictly separate files
    logger.propagate = False
    return logger

# 1. Scrap Logs (Browser interactions)
scrap_logger = setup_logger('scrap', os.path.join(LOGS_DIR, 'scrap.logs'))
# 2. Pipeline Logs (Run flow, LLM, Tracxn JSON)
pipeline_logger = setup_logger('pipeline', os.path.join(LOGS_DIR, 'pipeline.logs'))
# 3. System Integrity Logs (Health metrics)
system_logger = setup_logger('system', os.path.join(LOGS_DIR, 'system.logs'))
# 4. Root Logger (API connections and general)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, 'api.logs'), mode="a")
    ]
)

# Snapshots directory for debug mode
SNAPSHOTS_DIR = os.path.join(LOGS_DIR, 'Snapshots')
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

async def log_system_metrics():
    """Background task to log system integrity metrics every 60 seconds."""
    while True:
        try:
            cpu = psutil.cpu_percent()
            mem = psutil.virtual_memory().percent
            disk = psutil.disk_usage('/').percent
            load = os.getloadavg() if hasattr(os, 'getloadavg') else (0,0,0)
            system_logger.info(f"HEALTH: CPU: {cpu}% | RAM: {mem}% | DISK: {disk}% | LOAD: {load}")
        except Exception as e:
            system_logger.error(f"HEALTH_ERR: {str(e)}")
        await asyncio.sleep(60)

gemini_limiter = RateLimiter(2000)
tracxn_limiter = RateLimiter(160)

# Global semaphore to limit total parallel browser contexts to prevent CPU spikes
BROWSER_SEMAPHORE = asyncio.Semaphore(CONFIG.get("MAX_CONCURRENT_BROWSERS", 3))

async def save_snapshot(domain: str, html: str, reason: str):
    """Saves HTML snapshot for debugging purposes."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_domain = re.sub(r'[^\w\-]', '_', domain)
        filename = f"{safe_domain}_{reason}_{ts}.html"
        filepath = os.path.join(SNAPSHOTS_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
        scrap_logger.info(f"SNAPSHOT: Saved {filename} for debugging")
    except Exception as e:
        scrap_logger.error(f"SNAPSHOT_ERR: Could not save snapshot: {e}")

async def fetch_page(browser, url: str) -> Tuple[Optional[str], int, str]:
    scrap_logger.info(f"FETCH START: {url}")
    
    # --- TIER 0: BASIC HTTPX (User's working method) ---
    try:
        import httpx
        scrap_logger.info(f"TIER 0: Basic HTTPX Fetch for {url}")
        async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=45) as client:
            h_resp = await client.get(url, headers=MAIN_HEADERS)
            if h_resp.status_code == 200:
                content = h_resp.text
                if "sgcaptcha" not in content.lower() and len(content) > 1000:
                    scrap_logger.info(f"TIER 0 SUCCESS: {url} | Chars: {len(content)}")
                    return content, 200, "Success"
                scrap_logger.warning(f"TIER 0 FAIL: {url} | Reason: Captcha or Low Content")
            else:
                scrap_logger.warning(f"TIER 0 FAIL: {url} | Status: {h_resp.status_code}")
    except Exception as e:
        scrap_logger.warning(f"TIER 0 ERR: {url} | {str(e)}")

    # --- TIER 1: SCRAPLING (Fast Request Stealth) ---
    try:
        from scrapling import Fetcher
        scrap_logger.info(f"TIER 1: Scrapling Request Fetch for {url}")
        # NEW: use configure() to avoid deprecation warnings
        
        s_fetcher = Fetcher()
        s_resp = s_fetcher.get(url)
        
        if s_resp.status == 200:
            content = s_resp.text
            if "sgcaptcha" not in content.lower() and len(content) > 1000:
                scrap_logger.info(f"TIER 1 SUCCESS: {url} | Chars: {len(content)}")
                return content, 200, "Success"
            scrap_logger.warning(f"TIER 1 FAIL: {url} | Reason: Captcha or Low Content")
        else:
            scrap_logger.warning(f"TIER 1 FAIL: {url} | Status: {s_resp.status}")
    except Exception as e:
        scrap_logger.warning(f"TIER 1 ERR: {url} | {str(e)}")

    # --- TIER 2: CAMOUFOX (Full Browser) ---
    for camoufox_attempt in range(2):
        context = None
        try:
            async with BROWSER_SEMAPHORE:
                scrap_logger.info(f"TIER 2: Camoufox Browser Fetch for {url} (attempt {camoufox_attempt + 1})")
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                
                # EXPLICIT MEDIA BLOCKING
                async def block_media(route):
                    if route.request.resource_type in ["image", "media", "font", "object", "texttrack", "manifest", "other"]:
                        await route.abort()
                    else:
                        await route.continue_()
                
                await page.route("**/*", block_media)
                
                response = await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                
                if response and response.status == 200:
                    await asyncio.sleep(5)
                    content = await page.content()
                    if "sgcaptcha" not in content.lower() and len(content) > 500:
                        scrap_logger.info(f"TIER 2 SUCCESS: {url} | Chars: {len(content)}")
                        return content, 200, "Success"
                    scrap_logger.warning(f"TIER 2 FAIL: {url} | Reason: Captcha or Low Content")
                else:
                    scrap_logger.warning(f"TIER 2 FAIL: {url} | Status: {response.status if response else 'No Resp'}")
                break
        except Exception as e:
            err_msg = str(e)
            scrap_logger.warning(f"TIER 2 ERR: {url} | {err_msg} (attempt {camoufox_attempt + 1})")
            if "Proxy" in err_msg and camoufox_attempt == 0:
                scrap_logger.info(f"TIER 2 RETRY: Proxy failure, retrying once for {url}")
                await asyncio.sleep(2)
                continue
        finally:
            if context:
                await context.close()

    # --- TIER 3: SCRAPLING STEALTH (Playwright-backed Stealth) ---
    try:
        async with BROWSER_SEMAPHORE:
            from scrapling import StealthyFetcher
            scrap_logger.info(f"TIER 3: Scrapling Stealth (Playwright) for {url}")
            # NEW: use async_fetch class method to avoid sync-loop crash
            s_resp = await StealthyFetcher.async_fetch(url, headless=True, timeout=CONFIG["REQUEST_TIMEOUT"] * 1000)
            
            if s_resp.status == 200:
                content = s_resp.text
                if "sgcaptcha" not in content.lower():
                    scrap_logger.info(f"TIER 3 SUCCESS: {url} | Chars: {len(content)}")
                    return content, 200, "Success"
                return None, 200, "Captcha Blocked"
            else:
                return None, s_resp.status, f"Status {s_resp.status}"
    except Exception as e:
        scrap_logger.error(f"TIER 3 ERR: {url} | {str(e)}")
        return None, 0, "Fetch failed"

    return None, 0, "Fetch failed"


def extract_links(html: str, base_url: str) -> Set[str]:
    if not html: return set()
    soup = BeautifulSoup(html, 'lxml')
    links = set()
    domain = urlparse(base_url).netloc
    for a in soup.find_all('a', href=True):
        url = urljoin(base_url, a['href'])
        if urlparse(url).netloc == domain:
            links.add(url.split('#')[0].rstrip('/'))
    return links

async def process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map) -> Dict:
    domain = row[h_map["domain"]]
    pipeline_logger.info(f"PROCESS START: {domain}")
    dp_id, funnel_id, hashtags = row[h_map["dp_id"]], row[h_map["funnel_id"]], [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else []
    
    html, _, reason = await fetch_page(browser, f"https://{domain}")
    if not html: 
        pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: {reason}")
        return {"type": "error", "reason": reason}
    
    home_text = clean_html(html)
    parked, kw = is_parked_domain(html, home_text)
    if parked:
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked ({kw})")
        return {"type": "error", "reason": "Parked"}
    
    body_results = [home_text]
    links = extract_links(html, f"https://{domain}")
    
    target_urls = []
    scraped_urls = {f"https://{domain}"}
    for group in paths:
        for p in group:
            m = next((l for l in links if p in l and l not in scraped_urls), None)
            if m: target_urls.append(m); scraped_urls.add(m); break
    
    if target_urls:
        pipeline_logger.info(f"PROCESS: Fetching {len(target_urls)} sub-pages for {domain}")
        res = await asyncio.gather(*[fetch_page(browser, u) for u in target_urls])
        body_results.extend([clean_html(r[0]) for r in res if r[0]])
    
    combined = "\n\n".join(body_results)
    pipeline_logger.info(f"PROCESS: Combined body length: {len(combined)}")
    
    p1 = prompts[0].replace("XX", combined[:CONFIG["MAX_PROMPT_SIZE"]])
    res_p1_obj = await call_gemini_api(session, p1, gemini_limiter)
    res_p1 = res_p1_obj.text
    in1, out1 = res_p1_obj.prompt_tokens, res_p1_obj.candidate_tokens
    sd, ld1 = extract_descriptions(res_p1)
    if sd == "NO_DATA":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Insufficient content (AI reported NO_DATA)")
        return {"type": "error", "reason": "Low content"}
    if sd == "PARKED_LLM":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked (AI reported PARKED_LLM)")
        return {"type": "error", "reason": "Parked"}
    if not sd or not ld1:
        pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: LLM failed to generate descriptions")
        return {"type": "error", "reason": "LLM failed to generate descriptions"}

    p2 = prompts[1].replace("XX", combined[:CONFIG["MAX_PROMPT_SIZE"]]).replace("YY", sd)
    res_p2_obj = await call_gemini_api(session, p2, gemini_limiter)
    res_p2 = res_p2_obj.text
    in2, out2 = res_p2_obj.prompt_tokens, res_p2_obj.candidate_tokens
    _, ld2 = extract_descriptions(res_p2)
    
    ld_main = f"{ld1}\n\n{ld2}"
    feed = row[h_map["feed"]].split(" : ")[1] if " : " in row[h_map["feed"]] else row[h_map["feed"]]
    f_id, f_def = f_ids.get(feed, ""), f_defs.get(feed, "")
    
    # BM Logic
    pipeline_logger.info(f"PROCESS: Running BM prediction for {domain}")
    bm_p1 = prompts[6].replace("YY", ld_main).replace("XX", f_def).replace("BM_Paths", "\n".join([f"{r[0]}. {r[1]} - {r[2]}" for r in bm_mapping.get(feed, {}).get("1stLevel", [])]))
    res_bm1_obj = await call_gemini_api(session, bm_p1, gemini_limiter)
    res_bm1 = res_bm1_obj.text
    in3, out3 = res_bm1_obj.prompt_tokens, res_bm1_obj.candidate_tokens
    bm_name_1 = "No Results"
    m = re.search(r'^\d+[\.\s]+\s*(.*?)\s*[,:-]\s*Explanation', res_bm1, re.M)
    if m: bm_name_1 = m.group(1).strip()
    
    bm_name_2, bm_id_2, res_bm2, bm_p2, in4, out4 = "No BM matched", "No ID", "", "", 0, 0
    f_bms2 = bm_mapping.get(feed, {}).get("2ndLevel", [])
    if bm_name_1 != "No Results" and f_bms2:
        filt = [s for s in f_bms2 if s[2].startswith(bm_name_1)]
        if filt:
            bm_p2 = prompts[7].replace("XX", ld_main).replace("BM_Paths", "\n".join([" ".join(map(str, r)) for r in filt]))
            res_bm2_obj = await call_gemini_api(session, bm_p2, gemini_limiter)
            res_bm2 = res_bm2_obj.text
            in4, out4 = res_bm2_obj.prompt_tokens, res_bm2_obj.candidate_tokens
            m2 = re.search(r'^\d+[\.\s]+\s*(.*?)\s*[,:-]\s*Explanation', res_bm2, re.M)
            if m2: bm_name_2 = m2.group(1).strip(); bm_id_2 = bm_ids.get(bm_name_2, "No ID")
            elif bm_1st_stat.get(bm_name_1) == "Live": bm_name_2, bm_id_2 = bm_name_1, bm_ids.get(bm_name_1, "No ID")

    pipeline_logger.info(f"PROCESS SUCCESS: {domain} | BM: {bm_name_2}")
    return {
        "type": "success", "dp_id": dp_id, "funnel_id": funnel_id, "hashtags": hashtags,
        "sd": sd, "ld1": ld1, "ld2": ld2, "bmp1": bm_p1[:40000], "bmr1": res_bm1[:40000], "bmp2": bm_p2[:40000], "bmr2": res_bm2[:40000], "bm_name": bm_name_2, "bm_id": bm_id_2, "sf": ", ".join(hashtags), "feed_id": f_id,
        "tokens": {"in": in1+in2+in3+in4, "out": out1+out2+out3+out4}, "body_len": len(combined)
    }

class TypeAPipeline:
    def __init__(self, start_row: int, mode: str):
        self.start_row = start_row
        self.mode = mode
        self.config = CONFIG.copy()
        self.apply_formatting = True

    async def run(self):
        pipeline_logger.info(f"PIPELINE START: Row {self.start_row} | Mode: {self.mode}")
        self.report_progress(0, 0, 0, 0)
        # Start system monitoring
        asyncio.create_task(log_system_metrics())
        
        pipeline_logger.info("Connecting to Google Sheets...")
        gc = await GoogleSheetsClient.get_manager(self.config["CREDENTIALS_FILE"]).authorize()
        sheet = await gc.open_by_key(self.config["SHEET_ID"])
        ws = await sheet.worksheet(self.config["EXTRACTING_SHEET_NAME"])
        
        pipeline_logger.info(f"Fetching data from Row {self.start_row}...")
        # Optimize: Only fetch from start_row onwards to avoid memory/timeout issues with large sheets
        all_rows = await ws.get_values(f"A{self.start_row}:Z")
        data_rows = [r for r in all_rows if len(r) > 1 and r[1].strip()]
        total = len(data_rows)
        pipeline_logger.info(f"Total rows to process: {total}")
        self.report_progress(0, total, 0, 0)
        h_map = {
            "domain": 1, "dp_id": 2, "feed": 3, "funnel_id": 4, "tags": 5,
            "skip": 7, "sd": 9, "ld1": 10, "ld2": 11, "feed_id": 19
        }
        
        pipeline_logger.info("Connecting to Master Sheet...")
        m_sheet = await gc.open_by_key(self.config["MASTER_SHEET_ID"])
        f_lvl = await (await m_sheet.worksheet("1st Level")).get_all_values()
        s_lvl = await (await m_sheet.worksheet("2nd Level Live BM's")).get_all_values()
        
        bm_mapping, bm_ids, bm_1st_stat = {}, {}, {}
        for r in f_lvl[1:]:
            if len(r) < 5: continue
            f, p, bid, stat, desc = r[0], r[1], r[2], r[3], r[4]
            bm_ids[p], bm_1st_stat[p] = bid, stat
            if f not in bm_mapping: bm_mapping[f] = {"1stLevel":[], "2ndLevel":[]}
            bm_mapping[f]["1stLevel"].append([len(bm_mapping[f]["1stLevel"])+1, p, desc])
        for r in s_lvl[1:]:
            if len(r) < 4: continue
            f, p, bid, desc = r[0], r[1], r[2], r[3]
            bm_ids[p] = bid
            if f not in bm_mapping: bm_mapping[f] = {"1stLevel":[], "2ndLevel":[]}
            bm_mapping[f]["2ndLevel"].append([len(bm_mapping[f]["2ndLevel"])+1, ".", p, " -"+desc])
 
        pipeline_logger.info("Connecting to Prompts & Feed Owner sheets...")
        prompts = [r[1] for r in (await (await (await gc.open_by_key(CONFIG["PROMPTS_SHEET_ID"])).worksheet("Prompts")).get_all_values())[1:10]]
        f_ids = {r[0]: r[1] for r in (await (await (await gc.open_by_key(CONFIG["FEED_OWNER_SHEET_ID"])).worksheet("Feed Owner Details")).get_all_values())}
        
        pipeline_logger.info("Connecting to Feed Definition sheets...")
        f_defs = {}
        for sid in [CONFIG["FEED_DEF_SHEET_ID_1"], CONFIG["FEED_DEF_SHEET_ID_2"]]:
            try:
                fd_data = await (await (await gc.open_by_key(sid)).worksheet("Feed Definition")).get_all_values()
                for r in fd_data[1:]:
                    if len(r) > 3: f_defs[r[1]] = r[3]
            except: pass
            
        pipeline_logger.info("Connecting to Paths worksheet...")
        # Optimize: Get only first 20 columns of Paths
        paths = [[c for c in r if c.strip()] for r in (await (await sheet.worksheet("Paths")).get_values("A1:T50")) if any(r)]

        work_queue, result_queue = asyncio.Queue(), asyncio.Queue()
        for idx, row in enumerate(data_rows, start=self.start_row):
            if len(row) > h_map["skip"] and row[h_map["skip"]] == "Yes": continue
            await work_queue.put((idx, row))

        async with aiohttp.ClientSession() as session:
            if self.mode == "phase2":
                tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, None, session, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
                writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows)))
                await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()
            else:
                async with AsyncCamoufox(
                    headless=True,
                    humanize=True,
                    block_webrtc=True,
                    os="windows",
                    i_know_what_im_doing=True
                ) as browser:
                    tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, browser, session, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
                    writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows)))
                    await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()

    async def domain_worker(self, w_q, r_q, browser, session, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map):
        monitor = SystemHealthMonitor()
        while True:
            idx, row = await w_q.get()
            try:
                await monitor.wait_for_resources(logger=pipeline_logger)
                domain = row[h_map["domain"]]
                date_str = datetime.now().strftime("%d-%b-%Y")
                await r_q.put({'range': f"A{idx}", 'values': [[date_str]]})
                if self.mode == "phase2":
                    sd, ld1, dp_id, funnel_id = row[h_map["sd"]], row[h_map["ld1"]], row[h_map["dp_id"]], row[h_map["funnel_id"]]
                    scrap_stat = row[h_map["domain"] + 7] if len(row) > 8 else ""
                    if not (sd and ld1 and dp_id and funnel_id and scrap_stat.startswith("Yes")):
                        res = {"type": "failed", "reason": "Missing Phase 1 inputs"}
                    else:
                        res = {
                            "type": "success", "dp_id": dp_id, "funnel_id": funnel_id, "sd": sd, "ld1": ld1,
                            "ld2": row[11], "bmp1": row[12], "bmr1": row[13], "bmp2": row[14], "bmr2": row[15],
                            "bm_name": row[16], "bm_id": row[17], "sf": row[18], "feed_id": row[19],
                            "hashtags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
                            "tokens": {"in":0, "out":0}, "body_len": int(scrap_stat.split(":")[-1]) if ":" in scrap_stat else 0
                        }
                else: res = await process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map)
                
                if res["type"] == "success":
                    if self.mode != "phase2":
                        await r_q.put({'range': f"I{idx}:T{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld1"], res["ld2"], res["bmp1"], res["bmr1"], res["bmp2"], res["bmr2"], res["bm_name"], res["bm_id"], res["sf"], res["feed_id"]]]})
                        await r_q.put({'range': f"X{idx}:Y{idx}", 'values': [[res["tokens"]["in"], res["tokens"]["out"]]]})
                    
                    if self.mode != "phase1":
                        pipeline_logger.info(f"PIPELINE: Updating Tracxn for {domain}")
                        tags = res["hashtags"] + ["bu_llm_sd_ld", "bu_Internal_SRprocess_TypeA"]
                        payload = {"id": res["dp_id"], "description": {"value": res["ld1"]}, "shortDescription": {"value": res["sd"]}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}}
                        s1, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, json_data=payload, headers=HEADERS)
                        edits = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                        
                        bm_up, fun_up = "N/A", "N/A"
                        f_name = row[h_map["feed"]]
                        f_id = res["feed_id"] or f_ids.get(res["bm_name"]) or f_ids.get(f_name)
                        if edits in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts") and res["bm_id"] != "No ID":
                            if f_id:
                                s2, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/3.0/w/theme-company-association", tracxn_limiter, json_data={"object": {"themeId": f_id, "status": "PUBLISHED", "businessModelId": res["bm_id"], "companyId": res["dp_id"]}, "opType": "Update"}, headers=HEADERS)
                                bm_up = "Done" if s2 in (200, 201) else ("Duplicate/Already Moved" if s2 == 422 else ("Funnel State Conflicts" if s2 == 400 else str(s2)))
                                
                                if bm_up in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts"):
                                    ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": ["5dc5863a2799a51cc0ff30e2"], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                                    fun_up = "Done" if ms in (200, 201) else ("Duplicate/Already Moved" if ms == 422 else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        await r_q.put({'range': f"T{idx}:W{idx}", 'values': [[f_id, edits, bm_up, fun_up]]})
                        await r_q.put({'type': 'progress', 'is_success': (edits == "Done")})
                    else: await r_q.put({'type': 'progress', 'is_success': True})
                else:
                    reason = res.get('reason', 'Failed')
                    pipeline_logger.error(f"PIPELINE FAILED: {domain} | {reason}")
                    if self.mode != "phase2":
                        await r_q.put({'range': f"I{idx}", 'values': [[reason]]})
                    await r_q.put({'type': 'progress', 'is_success': False})
            except Exception as e:
                pipeline_logger.error(f"PIPELINE EXC: {str(e)}")
            finally: w_q.task_done()

    async def sheet_writer(self, r_q, ws, total):
        processed, s, f = set(), 0, 0
        while True:
            updates = []
            while not r_q.empty() and len(updates) < CONFIG["BATCH_SIZE"]:
                item = await r_q.get()
                if isinstance(item, dict) and item.get('type') == 'progress':
                    if item.get('is_success'): s += 1
                    else: f += 1
                    r_q.task_done(); continue
                updates.append(item)
            if updates:
                try:
                    await ws.batch_update(updates, value_input_option='USER_ENTERED')
                    for u in updates:
                        m = re.search(r'\d+', u['range'])
                        if m: processed.add(int(m.group()))
                except Exception as e:
                    logging.error(f"Error while calling batch_update: {e}")
                finally:
                    for _ in updates: r_q.task_done()
                    self.report_progress(len(processed), total, s, f)
            else: self.report_progress(len(processed), total, s, f)
            await asyncio.sleep(1)

    def report_progress(self, curr, total, s, f):
        try:
            with open(".progress.json", "w") as file: json.dump({"current": curr, "total": total, "success": s, "fail": f}, file)
        except: pass

async def main():
    import sys
    row = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    mode = sys.argv[2] if len(sys.argv) > 2 else "full"
    
    # Check for sheet_id and no-format
    sheet_id = None
    if "--sheet_id" in sys.argv:
        idx = sys.argv.index("--sheet_id")
        if idx + 1 < len(sys.argv):
            sheet_id = sys.argv[idx + 1]
    
    apply_formatting = True
    if "--no-format" in sys.argv:
        apply_formatting = False
        
    pipeline = TypeAPipeline(row, mode)
    if sheet_id:
        pipeline.config["SHEET_ID"] = sheet_id
    pipeline.apply_formatting = apply_formatting
    await pipeline.run()

if __name__ == "__main__": asyncio.run(main())
