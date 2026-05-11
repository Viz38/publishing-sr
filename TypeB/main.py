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
from gspread_formatting import set_frozen
from typing import Dict, List, Set, Tuple, Optional, Union, Any
from urllib.parse import urlparse
from dotenv import load_dotenv

from sr_common.config import settings
from sr_common.clients import RateLimiter, GoogleSheetsClient
from sr_common.utils import call_gemini_api, call_tracxn_api, clean_html, extract_descriptions, is_parked_domain

# Configuration
CONFIG = {
    "SHEET_ID": settings.TYPEB_SHEET_ID,
    "MASTER_SHEET_ID": settings.MASTER_SHEET_ID,
    "PROMPTS_SHEET_ID": settings.PROMPTS_SHEET_ID,
    "FEED_OWNER_SHEET_ID": settings.FEED_OWNER_SHEET_ID,
    "FEED_DEF_SHEET_ID_1": settings.FEED_DEF_SHEET_ID_1,
    "FEED_DEF_SHEET_ID_2": settings.FEED_DEF_SHEET_ID_2,
    "BM_MAPPING_SHEET_ID": "1kZpQYmsJTjNrNs3COfBPqJ9ne1rC1Lwlaw92YYMpjXo",
    "EXTRACTING_SHEET_NAME": "DB",
    "CREDENTIALS_FILE": os.path.join(os.path.dirname(os.path.abspath(__file__)), "TypeB.json"),
    "MAX_WORKERS": 3,
    "MAX_CONCURRENT_BROWSERS": 3,
    "GEMINI_API_URL": settings.GEMINI_API_URL,
    "GEMINI_API_KEY": settings.TYPEB_GEMINI_API_KEY,
    "MAX_PROMPT_SIZE": settings.MAX_PROMPT_SIZE,
    "BATCH_SIZE": settings.BATCH_SIZE,
    "REQUEST_TIMEOUT": settings.REQUEST_TIMEOUT,
    "MAX_RETRIES": settings.MAX_RETRIES,
    "RETRY_DELAY": settings.RETRY_DELAY
}

HEADERS = {
    "accessToken": settings.TYPEB_TRACXN_ACCESS_TOKEN,
    "Content-Type": "application/json",
    "X-Request-Source": 'Type-B-Publishing'
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
    logger.propagate = False
    return logger

# 1. Scrap Logs
scrap_logger = setup_logger('scrap', os.path.join(LOGS_DIR, 'scrap.logs'))
# 2. Pipeline Logs
pipeline_logger = setup_logger('pipeline', os.path.join(LOGS_DIR, 'pipeline.logs'))
# 3. System Integrity Logs
system_logger = setup_logger('system', os.path.join(LOGS_DIR, 'system.logs'))
# 4. Root Logger (API)
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

from urllib.parse import urlparse

# Global semaphore to limit total parallel browser contexts to prevent CPU spikes
BROWSER_SEMAPHORE = asyncio.Semaphore(CONFIG.get("MAX_CONCURRENT_BROWSERS", 3))

async def fetch_page(browser, url: str) -> Tuple[Optional[str], int, str]:
    scrap_logger.info(f"FETCH START: {url}")
    
    # --- TIER 0: BASIC HTTPX ---
    try:
        import httpx
        scrap_logger.info(f"TIER 0: Basic HTTPX Fetch for {url}")
        async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=30) as client:
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

    # --- TIER 1: SCRAPLING ---
    try:
        from scrapling import Fetcher
        scrap_logger.info(f"TIER 1: Scrapling Request Fetch for {url}")
        Fetcher.configure(timeout=60, verify=False)
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

    # --- TIER 2: CAMOUFOX ---
    context = None
    try:
        async with BROWSER_SEMAPHORE:
            scrap_logger.info(f"TIER 2: Camoufox Browser Fetch for {url}")
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()
            
            # EXPLICIT MEDIA BLOCKING
            async def block_media(route):
                if route.request.resource_type in ["image", "media", "font"]:
                    await route.abort()
                else:
                    await route.continue_()
            
            await page.route("**/*", block_media)
            
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            if response and response.status == 200:
                await asyncio.sleep(5)
                content = await page.content()
                if "sgcaptcha" not in content.lower() and len(content) > 500:
                    scrap_logger.info(f"TIER 2 SUCCESS: {url} | Chars: {len(content)}")
                    return content, 200, "Success"
                scrap_logger.warning(f"TIER 2 FAIL: {url} | Reason: Captcha or Low Content")
            else:
                scrap_logger.warning(f"TIER 2 FAIL: {url} | Status: {response.status if response else 'No Resp'}")
    except Exception as e:
        scrap_logger.warning(f"TIER 2 ERR: {url} | {str(e)}")
    finally:
        if context:
            await context.close()

    # --- TIER 3: SCRAPLING STEALTH ---
    try:
        async with BROWSER_SEMAPHORE:
            from scrapling import StealthyFetcher
            scrap_logger.info(f"TIER 3: Scrapling Stealth (Playwright) for {url}")
            # NEW: use async_fetch class method to avoid sync-loop crash
            s_resp = await StealthyFetcher.async_fetch(url, headless=True, timeout=60)
            
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

async def process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map) -> Dict:
    domain = row[h_map["domain"]]
    pipeline_logger.info(f"PROCESS START: {domain}")
    
    html, _, reason = await fetch_page(browser, f"https://{domain}")
    if not html: 
        pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: {reason}")
        return {"type": "error", "reason": reason}
        
    body = clean_html(html)
    pipeline_logger.info(f"PROCESS: Scraped {domain} | Length: {len(body)}")
    
    # Check for parked
    parked, kw = is_parked_domain(html, body)
    if parked:
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked ({kw})")
        return {"type": "error", "reason": "Parked"}
    
    if len(body) < 100: 
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Low content")
        return {"type": "error", "reason": "Low content"}
        
    p1 = prompts[0].replace("XX", body[:20000])
    res_p1_obj = await call_gemini_api(session, p1, gemini_limiter)
    res_p1 = res_p1_obj.text
    in1, out1 = res_p1_obj.prompt_tokens, res_p1_obj.candidate_tokens
    sd, ld = extract_descriptions(res_p1)
    if sd == "NO_DATA":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Insufficient content (AI reported NO_DATA)")
        return {"type": "error", "reason": "Low content"}
    if sd == "PARKED_LLM":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked (AI reported PARKED_LLM)")
        return {"type": "error", "reason": "Parked"}
    
    if not sd or not ld: 
        pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: LLM failed to generate descriptions")
        return {"type": "error", "reason": "LLM failed"}
    
    feed = row[h_map["feed"]].split(" : ")[1] if " : " in row[h_map["feed"]] else row[h_map["feed"]]
    f_id, f_def = f_ids.get(feed, ""), f_defs.get(feed, "")
    
    pipeline_logger.info(f"PROCESS: Running BM prediction for {domain}")
    bm_res, bm_name, bm_id, f_chk, in2, out2 = "", "", None, "No", 0, 0
    if feed in bm_paths:
        bm_p = prompts[3].replace("XX", ld).replace("BMPathstr", "\n".join([" ".join(map(str, r)) for r in bm_paths[feed]["data"]])).replace("YY", f_def)
        res_bm_obj = await call_gemini_api(session, bm_p, gemini_limiter)
        bm_res, in2, out2 = res_bm_obj.text, res_bm_obj.prompt_tokens, res_bm_obj.candidate_tokens
        m = re.search(r'^(?:FeedOutput:\s*)?(Yes|No)', bm_res, re.I | re.M)
        f_chk = m.group(1) if m else "No"
        if f_chk == "Yes":
            num = re.search(r"\d+", bm_res)
            if num:
                bm_name = next((r[1] for r in bm_paths[feed]["data"] if str(r[0]) == num.group(0)), "")
                bm_id = bm_map.get(bm_name)

    pipeline_logger.info(f"PROCESS SUCCESS: {domain} | FeedCheck: {f_chk} | BM: {bm_name}")
    return {
        "type": "success", "dp_id": row[h_map["dp_id"]], "funnel_id": row[h_map["funnel_id"]], "hashtags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
        "sd": sd, "ld": ld[:40000], "bm_res": bm_res[:40000], "bm_name": bm_name, "bm_id": bm_id, "feedcheck": f_chk,
        "tokens": {"in": in1+in2, "out": out1+out2}, "feed_id": f_id, "body_len": len(body)
    }

class TypeBPipeline:
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
        # Optimize: Only fetch from start_row onwards
        all_rows = await ws.get_values(f"A{self.start_row}:Z")
        data_rows = [r for r in all_rows if len(r) > 1 and r[1].strip() and r[1].strip() not in ["TypeA", "TypeB", "TypeC"]]
        total = len(data_rows)
        pipeline_logger.info(f"Total rows to process: {total}")
        self.report_progress(0, total, 0, 0)
        h_map = {
            "domain": 1, "dp_id": 2, "feed": 3, "funnel_id": 4, "tags": 5,
            "skip": 6, "sd": 8, "ld": 9, "feed_id": 15
        }
        
        p_sheet = await gc.open_by_key(self.config["PROMPTS_SHEET_ID"])
        prompts = [r[1] for r in (await (await p_sheet.worksheet("Prompts")).get_all_values())[1:10]]
        fo_sheet = await gc.open_by_key(self.config["FEED_OWNER_SHEET_ID"])
        f_ids = {r[0]: r[1] for r in (await (await fo_sheet.worksheet("Feed Owner Details")).get_all_values())}
        bm_sheet = await gc.open_by_key(self.config["BM_MAPPING_SHEET_ID"])
        bm_data = await (await bm_sheet.worksheet("BM's & Definition")).get_all_values()
        bm_map = {r[1]: r[2] for r in bm_data[1:] if len(r) > 2}
        bm_paths = {}
        for r in bm_data[1:]:
            f = r[1].split(">")[0]
            if f not in bm_paths: bm_paths[f] = {"data": []}
            bm_paths[f]["data"].append([len(bm_paths[f]["data"])+1, r[1], r[3]])
        f_defs = {}
        for sid in [self.config["FEED_DEF_SHEET_ID_1"], self.config["FEED_DEF_SHEET_ID_2"]]:
            try:
                fd_data = await (await (await gc.open_by_key(sid)).worksheet("Feed Definition (Worked)")).get_all_values()
                for r in fd_data[1:]:
                    if len(r) > 4: f_defs[r[1]] = r[4]
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
                tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, None, session, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
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
                    tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, browser, session, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
                    writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows)))
                    await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()

    async def domain_worker(self, w_q, r_q, browser, session, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map):
        while True:
            idx, row = await w_q.get()
            try:
                domain = row[h_map["domain"]]
                date_str = datetime.now().strftime("%d-%b-%Y")
                await r_q.put({'range': f"A{idx}", 'values': [[date_str]]})
                if self.mode == "phase2":
                    sd, ld, dp_id, funnel_id = row[h_map["sd"]], row[h_map["ld"]], row[h_map["dp_id"]], row[h_map["funnel_id"]]
                    scrap_stat = row[7] if len(row) > 7 else ""
                    if not (sd and ld and dp_id and funnel_id and scrap_stat.startswith("Yes")):
                        res = {"type": "failed", "reason": "Missing Phase 1 inputs"}
                    else:
                        res = {
                            "type": "success", "dp_id": dp_id, "funnel_id": funnel_id, "sd": sd, "ld": ld,
                            "feed_id": row[15], "feedcheck": row[10], "bm_res": row[11], "bm_name": row[12], "bm_id": row[13],
                            "hash_status": row[14] if len(row) > 14 else "N/A",
                            "hashtags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
                            "tokens": {"in":0, "out":0}, "body_len": int(scrap_stat.split(":")[-1]) if ":" in scrap_stat else 0
                        }
                else: res = await process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map)
                
                if res["type"] == "success":
                    if self.mode != "phase2":
                        hash_stat = "Yes" if res.get("hash_removed") else "No"
                        await r_q.put({'range': f"H{idx}:P{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld"], res["feedcheck"], res["bm_res"], res["bm_name"], res["bm_id"], hash_stat, res["feed_id"]]]})
                        await r_q.put({'range': f"T{idx}:U{idx}", 'values': [[res["tokens"]["in"], res["tokens"]["out"]]]})
                    else:
                        hash_stat = res.get("hash_status", "N/A")
                    
                    if self.mode != "phase1":
                        pipeline_logger.info(f"PIPELINE: Updating Tracxn for {domain}")
                        tags = res["hashtags"] + ["bu_llm_sd_ld", "llmbasedpublishing"]
                        if res["feedcheck"] == "Yes": tags.append("bu_llm_businessmodel_prediction")
                        sdld, bm, fun = "N/A", "N/A", "N/A"
                        f_id = res["feed_id"] or f_ids.get(res["bm_name"]) or f_ids.get(row[3] if len(row) > 3 else "")
                        if res["feedcheck"] == "Yes" and res["bm_id"]:
                            s1, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data={"id": res["dp_id"], "description": {"value": res["ld"]}, "shortDescription": {"value": res["sd"]}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}}, headers=HEADERS)
                            s2, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/3.0/w/theme-company-association", tracxn_limiter, method="put", json_data={"object": {"themeId": f_id, "status": "PUBLISHED", "businessModelId": res["bm_id"], "companyId": res["dp_id"]}, "opType": "Update"}, headers=HEADERS)
                            sdld = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                            bm = "Done" if s2 in (200, 201) else ("Duplicate/Already Moved" if s2 == 422 else ("Funnel State Conflicts" if s2 == 400 else str(s2)))
                            
                            if sdld in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts") and bm in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts"):
                                ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": ["5dc5863a2799a51cc0ff30e2"], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                                fun = "Done" if ms in (200, 201) else ("Duplicate/Already Moved" if ms == 422 else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        else:
                            s1, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data={"id": res["dp_id"], "description": {"value": res["ld"]}, "shortDescription": {"value": res["sd"]}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}}, headers=HEADERS)
                            sdld = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                            ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": ["591d37b884ae06633a652496"], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                            fun = "Sent discovery" if ms in (200, 201) else ("Duplicate/Already Moved" if ms == 422 else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        await r_q.put({'range': f"O{idx}:S{idx}", 'values': [[hash_stat, f_id, sdld, bm, fun]]})
                        await r_q.put({'type': 'progress', 'is_success': sdld in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts")})
                    else: await r_q.put({'type': 'progress', 'is_success': True})
                else:
                    reason = res.get('reason', 'Failed')
                    pipeline_logger.error(f"PIPELINE FAILED: {domain} | {reason}")
                    if self.mode != "phase2":
                        await r_q.put({'range': f"H{idx}", 'values': [[reason]]})
                    await r_q.put({'type': 'progress', 'is_success': False})
            except Exception as e:
                pipeline_logger.error(f"PIPELINE EXC: {str(e)}")
            finally: w_q.task_done()

    async def sheet_writer(self, r_q, ws, total):
        processed_indices, success, fail = set(), 0, 0
        while True:
            updates = []
            while not r_q.empty() and len(updates) < CONFIG["BATCH_SIZE"]:
                item = await r_q.get()
                if isinstance(item, dict) and item.get('type') == 'progress':
                    if item.get('is_success'): success += 1
                    else: fail += 1
                    r_q.task_done(); continue
                updates.append(item)
            if updates:
                try:
                    await ws.batch_update(updates, value_input_option='USER_ENTERED')
                    for u in updates:
                        match = re.search(r'\d+', u['range'])
                        if match: processed_indices.add(int(match.group()))
                except Exception as e:
                    pipeline_logger.error(f"SHEET WRITER ERR: {e}")
                finally:
                    for _ in updates: r_q.task_done()
                    self.report_progress(len(processed_indices), total, success, fail)
                    pipeline_logger.info(f"PROGRESS: {len(processed_indices)}/{total} | Success: {success} | Fail: {fail}")
            else: self.report_progress(len(processed_indices), total, success, fail)
            await asyncio.sleep(1)

    def report_progress(self, curr, total, s, f):
        try:
            with open(".progress.json", "w") as file: json.dump({"current": curr, "total": total, "success": s, "fail": f}, file)
        except: pass

async def main():
    import sys
    row = int(sys.argv[1]) if len(sys.argv) > 1 else 3
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
        
    pipeline = TypeBPipeline(row, mode)
    if sheet_id:
        pipeline.config["SHEET_ID"] = sheet_id
    pipeline.apply_formatting = apply_formatting
    await pipeline.run()

if __name__ == "__main__": asyncio.run(main())
