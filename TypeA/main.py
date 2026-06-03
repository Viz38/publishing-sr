import asyncio
import aiohttp
from camoufox.async_api import AsyncCamoufox
from browserforge.fingerprints import Screen
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
from sr_common.utils import (
    call_gemini_api, call_tracxn_api, clean_html, extract_descriptions, 
    is_parked_domain, get_dynamic_max_workers, SystemHealthMonitor, 
    GeminiCacheManager
)
from sr_common.fetcher import StealthFetcher
from sr_common.stealth import get_browser_profile

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
    "TRACKING_SHEET_ID": "1OvBOAXc_Y5aDLcK-BGCALFUZyJWLYolmFkr3tmo7mj4",
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

# Global fetcher instance
fetcher = StealthFetcher()

def extract_links(html: str, base_url: str) -> Set[str]:
    if not html: return set()
    soup = BeautifulSoup(html, 'lxml')
    links = set()
    domain = urlparse(base_url).netloc.replace('www.', '')
    for a in soup.find_all('a', href=True):
        url = urljoin(base_url, a['href'])
        netloc = urlparse(url).netloc.replace('www.', '')
        if netloc == domain:
            links.add(url.split('#')[0].rstrip('/'))
    return links

async def process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map, cache_manager) -> Dict:
    domain = row[h_map["domain"]]
    pipeline_logger.info(f"PROCESS START: {domain}")
    dp_id, funnel_id, hashtags = row[h_map["dp_id"]], row[h_map["funnel_id"]], [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else []
    
    raw_data_col = h_map.get("raw_data")
    raw_data = row[raw_data_col] if raw_data_col is not None and len(row) > raw_data_col else ""
    
    if raw_data.strip():
        pipeline_logger.info(f"PROCESS: Found raw data for {domain}, skipping scrape.")
        final_url = f"https://{domain}"
        combined = raw_data.strip()
    else:
        html, final_url, reason = await fetcher.fetch(browser, f"https://{domain}")
        if html is None:
            pipeline_logger.warning(f"PROCESS FAILED HTTPS for {domain}. Retrying with HTTP...")
            html, final_url, reason = await fetcher.fetch(browser, f"http://{domain}")

        if html is None:
            pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: {reason}")
            return {"type": "error", "reason": reason}
        if len(html) < 300:
            pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: Low Content ({len(html)} chars)")
            return {"type": "error", "reason": "Low Content"}
        
        home_text = clean_html(html)
        parked, kw = is_parked_domain(html, home_text)
        if parked:
            pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked ({kw})")
            return {"type": "error", "reason": "Parked"}
        
        body_results = [home_text]
        links = extract_links(html, final_url)
        
        target_urls = []
        scraped_urls = {final_url}
        for group in paths:
            for p in group:
                m = next((l for l in links if p in l and l not in scraped_urls), None)
                if m: target_urls.append(m); scraped_urls.add(m); break
        
        if target_urls:
            pipeline_logger.info(f"PROCESS: Fetching {len(target_urls)} sub-pages for {domain}")
            res = await asyncio.gather(*[fetcher.fetch(browser, u) for u in target_urls])
            body_results.extend([clean_html(r[0]) for r in res if r[0]])
        
        combined = "\n\n".join(body_results)
    pipeline_logger.info(f"PROCESS: Combined body length: {len(combined)}")
    
    llm_calls = 0
    llm_rows = 1
    
    parts_p1 = prompts[0].split("XX")
    if len(parts_p1) == 2:
        sys_p1 = parts_p1[0].strip() + "\n\n[DATA PROVIDED BY USER BELOW]\n\n" + parts_p1[1].strip()
        user_p1 = "URL: " + str(final_url) + "\n\nRaw Content:\n" + combined[:CONFIG["MAX_PROMPT_SIZE"]]
        cache_id = await cache_manager.get_or_create(session, "prompt_0", sys_p1)
        res_p1_obj = await call_gemini_api(session, user_p1, gemini_limiter, system_instruction=sys_p1, cached_content_name=cache_id)
        bm_p1_raw = prompts[0].replace("XX", combined[:CONFIG["MAX_PROMPT_SIZE"]]) # for logging
    else:
        bm_p1_raw = prompts[0].replace("XX", combined[:CONFIG["MAX_PROMPT_SIZE"]])
        res_p1_obj = await call_gemini_api(session, bm_p1_raw, gemini_limiter)
    
    llm_calls += 1
    res_p1 = res_p1_obj.text
    in1, out1, think1 = res_p1_obj.prompt_tokens, res_p1_obj.candidate_tokens, res_p1_obj.thinking_tokens
    think_text = f"P1:\n{res_p1_obj.thinking_text}\n" if res_p1_obj.thinking_text else ""
    
    tokens = {"in": in1, "out": out1, "think": think1}
    
    sd, ld1 = extract_descriptions(res_p1)
    if sd == "NO_DATA":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Insufficient content (AI reported NO_DATA)")
        return {"type": "error", "reason": "Low content", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows}
    if sd == "PARKED_LLM":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked (AI reported PARKED_LLM)")
        return {"type": "error", "reason": "Parked", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows}
    if not sd or not ld1:
        pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: LLM failed to generate descriptions")
        return {"type": "error", "reason": "LLM failed", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows}

    parts_p2 = prompts[1].split("XX")
    if len(parts_p2) == 2:
        sys_p2 = parts_p2[0].strip() + "\n\n[DATA PROVIDED BY USER BELOW]\n\n" + parts_p2[1].strip()
        user_p2 = "Raw Content:\n" + combined[:CONFIG["MAX_PROMPT_SIZE"]]
        cache_id = await cache_manager.get_or_create(session, "prompt_1", sys_p2)
        res_p2_obj = await call_gemini_api(session, user_p2, gemini_limiter, system_instruction=sys_p2, cached_content_name=cache_id)
    else:
        p2 = prompts[1].replace("XX", combined[:CONFIG["MAX_PROMPT_SIZE"]]).replace("YY", sd)
        res_p2_obj = await call_gemini_api(session, p2, gemini_limiter)
    
    llm_calls += 1
    res_p2 = res_p2_obj.text
    in2, out2, think2 = res_p2_obj.prompt_tokens, res_p2_obj.candidate_tokens, res_p2_obj.thinking_tokens
    if res_p2_obj.thinking_text: think_text += f"P2:\n{res_p2_obj.thinking_text}\n"
    
    tokens["in"] += in2; tokens["out"] += out2; tokens["think"] += think2
    _, ld2 = extract_descriptions(res_p2)
    
    ld_main = f"{ld1}\n\n{ld2}"
    feed = row[h_map["feed"]].split(" : ")[1] if " : " in row[h_map["feed"]] else row[h_map["feed"]]
    f_id, f_def = f_ids.get(feed, ""), f_defs.get(feed, "")
    
    # BM Logic
    pipeline_logger.info(f"PROCESS: Running BM prediction for {domain}")
    bm_paths_str_1 = "\n".join([f"{r[0]}. {r[1]} - {r[2]}" for r in bm_mapping.get(feed, {}).get("1stLevel", [])])
    parts_bm1 = prompts[6].split("Company Description:\nYY")
    if len(parts_bm1) == 2:
        sys_bm1 = (parts_bm1[0].strip() + "\n\n[COMPANY DESCRIPTION PROVIDED BY USER BELOW]\n\n" + parts_bm1[1].strip()).replace("XX", f_def).replace("BM_Paths", bm_paths_str_1)
        user_bm1 = "Company Description:\n" + ld_main
        cache_key = f"prompt_6_{f_id}"
        cache_id = await cache_manager.get_or_create(session, cache_key, sys_bm1)
        res_bm1_obj = await call_gemini_api(session, user_bm1, gemini_limiter, system_instruction=sys_bm1, cached_content_name=cache_id)
        bm_p1 = prompts[6].replace("YY", ld_main).replace("XX", f_def).replace("BM_Paths", bm_paths_str_1) # for logging
    else:
        bm_p1 = prompts[6].replace("YY", ld_main).replace("XX", f_def).replace("BM_Paths", bm_paths_str_1)
        res_bm1_obj = await call_gemini_api(session, bm_p1, gemini_limiter)
    
    llm_calls += 1
    res_bm1 = res_bm1_obj.text
    pipeline_logger.debug(f"PROCESS: BM1 Response generated ({len(res_bm1)} chars)")
    in3, out3, think3 = res_bm1_obj.prompt_tokens, res_bm1_obj.candidate_tokens, res_bm1_obj.thinking_tokens
    if res_bm1_obj.thinking_text: think_text += f"BM1:\n{res_bm1_obj.thinking_text}\n"
    tokens["in"] += in3; tokens["out"] += out3; tokens["think"] += think3
    
    bm_name_1 = "No Results"
    if res_bm1.strip().startswith(("No Results", "No results")):
        bm_name_1 = "No Results"
    else:
        pattern = r'^\d+[\.\s]+\s*(.*?)\s*[,:-]\s*Explanation'
        m = re.search(pattern, res_bm1, re.MULTILINE)
        if m:
            bm_name_1 = m.group(1).strip()
        else:
            bm_name_1 = "No Results"
    
    bm_name_final, bm_id_final = "No BM matched", "No ID"
    bm_p2, res_bm2 = "", ""
    
    # Pre-fallback to 1st level if it's Live. BM2 will overwrite this if successful.
    if bm_name_1 != "No Results" and bm_1st_stat.get(bm_name_1) == "Live":
        bm_name_final, bm_id_final = bm_name_1, bm_ids.get(bm_name_1, "No ID")
        
    f_bms2 = bm_mapping.get(feed, {}).get("2ndLevel", [])
    if f_bms2:
        bm_p2 = prompts[7].replace("XX", ld_main)

    if bm_name_1 != "No Results" and f_bms2:
        filt = [s for s in f_bms2 if s[2].lower().startswith(bm_name_1.lower())]
        if filt:
            bm_paths_str_2 = "\n".join([" ".join(map(str, r)) for r in filt])
            parts_bm2 = prompts[7].split("Company Description:\nXX")
            if len(parts_bm2) == 2:
                sys_bm2 = (parts_bm2[0].strip() + "\n\n[COMPANY DESCRIPTION PROVIDED BY USER BELOW]\n\n" + parts_bm2[1].strip()).replace("BM_Paths", bm_paths_str_2)
                user_bm2 = "Company Description:\n" + ld_main
                cache_key = f"prompt_7_{bm_name_1.replace(' ', '_')}"
                cache_id = await cache_manager.get_or_create(session, cache_key, sys_bm2)
                res_bm2_obj = await call_gemini_api(session, user_bm2, gemini_limiter, system_instruction=sys_bm2, cached_content_name=cache_id)
                bm_p2 = prompts[7].replace("XX", ld_main).replace("BM_Paths", bm_paths_str_2)
            else:
                bm_p2 = prompts[7].replace("XX", ld_main).replace("BM_Paths", bm_paths_str_2)
                res_bm2_obj = await call_gemini_api(session, bm_p2, gemini_limiter)
            
            llm_calls += 1
            res_bm2 = res_bm2_obj.text
            pipeline_logger.debug(f"PROCESS: BM2 Response generated ({len(res_bm2)} chars)")
            in4, out4, think4 = res_bm2_obj.prompt_tokens, res_bm2_obj.candidate_tokens, res_bm2_obj.thinking_tokens
            if res_bm2_obj.thinking_text: think_text += f"BM2:\n{res_bm2_obj.thinking_text}\n"
            tokens["in"] += in4; tokens["out"] += out4; tokens["think"] += think4
            if res_bm2.strip().startswith(("No Results", "No results")):
                if bm_1st_stat.get(bm_name_1) == "Live":
                    bm_name_final = bm_name_1
                    bm_id_final = bm_ids.get(bm_name_1, "No ID")
                else:
                    bm_name_final = "No BM matched"
                    bm_id_final = "No ID"
            else:
                pattern = r'^\d+[\.\s]+\s*(.*?)\s*[,:-]\s*Explanation'
                m2 = re.search(pattern, res_bm2, re.MULTILINE)
                if m2:
                    bm_name_final = m2.group(1).strip()
                    bm_id_final = bm_ids.get(bm_name_final, "No ID")
                else:
                    if bm_1st_stat.get(bm_name_1) == "Live":
                        bm_name_final = bm_name_1
                        bm_id_final = bm_ids.get(bm_name_1, "No ID")
                    else:
                        bm_name_final = "No BM matched"
                        bm_id_final = "No ID"

    pipeline_logger.info(f"PROCESS SUCCESS: {domain} | BM: {bm_name_final}")
    return {
        "type": "success", "dp_id": dp_id, "funnel_id": funnel_id, "hashtags": hashtags,
        "sd": sd, "ld1": ld1, "ld2": ld2, "bmp1": bm_p1[:40000], "bmr1": res_bm1[:40000], "bmp2": bm_p2[:40000], "bmr2": res_bm2[:40000], "bm_name": bm_name_final, "bm_id": bm_id_final, "sf": "bu_llm_sd_ld, bu_Internal_SRprocess_TypeA", "feed_id": f_id,
        "tokens": tokens, "think_text": think_text,
        "llm_calls": llm_calls, "llm_rows": llm_rows,
        "body_len": len(combined)
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
        
        while True:
            try:
                pipeline_logger.info("Connecting to Google Sheets...")
                gc = await GoogleSheetsClient.get_manager(self.config["CREDENTIALS_FILE"]).authorize()
                sheet = await gc.open_by_key(self.config["SHEET_ID"])
                ws = await sheet.worksheet(self.config["EXTRACTING_SHEET_NAME"])
                
                pipeline_logger.info(f"Fetching data from Row {self.start_row}...")
                all_rows = await ws.get_values(f"A{self.start_row}:AB")
                data_rows = []
                for i, r in enumerate(all_rows):
                    if len(r) < 2: continue
                    real_idx = self.start_row + i
                    # Detect if row is a header or engine label
                    if r[1].strip() in ["TypeA", "TypeB", "TypeC", "Type A", "Type B", "Type C"]:
                        # Shifted sheet detection: Domain is in index 2
                        if len(r) > 2 and "." in r[2] and " " not in r[2].strip():
                            data_rows.append((real_idx, r))
                    elif "." in r[1] and " " not in r[1].strip():
                        # Standard sheet detection: Domain is in index 1
                        data_rows.append((real_idx, r))
                
                total = len(data_rows)
                pipeline_logger.info(f"Total rows to process: {total}")
                self.report_progress(0, total, 0, 0)
                break
            except Exception as e:
                pipeline_logger.error(f"Startup failed (Sheets Connection/DNS): {e}. Retrying in 10s...")
                await asyncio.sleep(10)
        
        # Determine mapping based on the first data row
        if data_rows:
            _, first_row = data_rows[0]
            if first_row[1].strip() in ["TypeA", "TypeB", "TypeC", "Type A", "Type B", "Type C"]:
                # Shifted mapping (Type A style - Columns shifted right by 1)
                h_map = {
                    "domain": 2, "dp_id": 3, "feed": 4, "funnel_id": 5, "tags": 6,
                    "skip": 8, "scrap_stat": 9, "sd": 10, "ld1": 11, "ld2": 12, "feed_id": 20,
                    "r1": "J", "r2": "U", "r3": "Y", # Standard Ranges: J-U, Y-AA
                    "raw_data": 26 # Col AA
                }
                pipeline_logger.info("Detected SHIFTED column mapping (Index 2 for Domain)")
            else:
                # Standard mapping
                h_map = {
                    "domain": 1, "dp_id": 2, "feed": 3, "funnel_id": 4, "tags": 5,
                    "skip": 7, "scrap_stat": 8, "sd": 9, "ld1": 10, "ld2": 11, "feed_id": 19,
                    "r1": "I", "r2": "T", "r3": "X", # Standard Ranges: I-T, X-Z
                    "raw_data": 26 # Col AA
                }
                pipeline_logger.info("Detected STANDARD column mapping (Index 1 for Domain)")
        else:
            h_map = {}
        
        pipeline_logger.info("Connecting to Master Sheet...")
        m_sheet = await gc.open_by_key(self.config["MASTER_SHEET_ID"])
        f_lvl = await (await m_sheet.worksheet("1st Level")).get_all_values()
        s_lvl = await (await m_sheet.worksheet("2nd Level Live BM's")).get_all_values()
        
        bm_mapping, bm_ids, bm_1st_stat = {}, {}, {}
        for r in f_lvl[1:]:
            if len(r) < 5: continue
            f, p, bid, stat, desc = r[0].strip(), r[1].strip(), r[2].strip(), r[3].strip(), r[4].strip()
            bm_ids[p], bm_1st_stat[p] = bid, stat
            if f not in bm_mapping: bm_mapping[f] = {"1stLevel":[], "2ndLevel":[]}
            bm_mapping[f]["1stLevel"].append([len(bm_mapping[f]["1stLevel"])+1, p, desc])
        for r in s_lvl[1:]:
            if len(r) < 4: continue
            f, p, bid, desc = r[0].strip(), r[1].strip(), r[2].strip(), r[3].strip()
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
        for idx, row in data_rows:
            if len(row) > h_map["skip"] and row[h_map["skip"]] == "Yes": continue
            await work_queue.put((idx, row))

        cache_manager = GeminiCacheManager(settings.TYPEA_GEMINI_API_KEY)
        async with aiohttp.ClientSession() as session:
            if self.mode == "phase2":
                tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, None, session, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map, cache_manager)) for _ in range(CONFIG["MAX_WORKERS"])]
                writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows), gc, "TypeA"))
                await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()
            else:
                while True:
                    try:
                        await SystemHealthMonitor(cpu_threshold=90, mem_threshold=90).wait_for_resources(logger=pipeline_logger)
                        profile = get_browser_profile("windows")
                        async with AsyncCamoufox(
                            headless=True,
                            humanize=True,
                            block_webrtc=True,
                            os=profile["os"],
                            screen=Screen(max_width=profile["screen_resolution"][0], max_height=profile["screen_resolution"][1]),
                            i_know_what_im_doing=True
                        ) as browser:
                            tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, browser, session, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map, cache_manager)) for _ in range(CONFIG["MAX_WORKERS"])]
                            writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows), gc, "TypeA"))
                            await work_queue.join()
                            [t.cancel() for t in tasks]
                            await result_queue.join()
                            writer_task.cancel()
                            break
                    except Exception as e:
                        pipeline_logger.error(f"BROWSER ENGINE CRASHED: {e}. Restarting...")
                        await asyncio.sleep(10)
                        await SystemHealthMonitor(cpu_threshold=80, mem_threshold=85).wait_for_resources(logger=pipeline_logger)

    async def domain_worker(self, w_q, r_q, browser, session, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map, cache_manager):
        monitor = SystemHealthMonitor()
        import random
        # Jitter start to prevent CPU storm
        j_time = random.uniform(1.0, 5.0)
        pipeline_logger.debug(f"WORKER: Jittering for {j_time:.1f}s...")
        await asyncio.sleep(j_time)
        while True:
            idx, row = await w_q.get()
            try:
                await monitor.wait_for_resources(logger=pipeline_logger)
                domain = row[h_map["domain"]]
                date_str = datetime.now().strftime("%d-%b-%Y")
                await r_q.put({'range': f"A{idx}", 'values': [[date_str]]})
                if self.mode == "phase2":
                    sd, ld1, dp_id, funnel_id = row[h_map["sd"]], row[h_map["ld1"]], row[h_map["dp_id"]], row[h_map["funnel_id"]]
                    r1_idx = ord(h_map["r1"]) - ord('A')
                    scrap_stat = row[r1_idx] if len(row) > r1_idx else ""
                    if not (sd and ld1 and dp_id and funnel_id and scrap_stat.startswith("Yes")):
                        res = {"type": "failed", "reason": "Missing Phase 1 inputs"}
                    else:
                        res = {
                            "type": "success", "dp_id": dp_id, "funnel_id": funnel_id, "sd": sd, "ld1": ld1,
                            "ld2": row[r1_idx + 3] if len(row) > r1_idx + 3 else "", 
                            "bmp1": row[r1_idx + 4] if len(row) > r1_idx + 4 else "", 
                            "bmr1": row[r1_idx + 5] if len(row) > r1_idx + 5 else "", 
                            "bmp2": row[r1_idx + 6] if len(row) > r1_idx + 6 else "", 
                            "bmr2": row[r1_idx + 7] if len(row) > r1_idx + 7 else "",
                            "bm_name": row[r1_idx + 8] if len(row) > r1_idx + 8 else "", 
                            "bm_id": row[r1_idx + 9] if len(row) > r1_idx + 9 else "", 
                            "sf": row[r1_idx + 10] if len(row) > r1_idx + 10 else "", 
                            "feed_id": row[r1_idx + 11] if len(row) > r1_idx + 11 else "",
                            "hashtags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
                            "tokens": {"in":0, "out":0, "think":0}, "body_len": int(scrap_stat.split(":")[-1]) if ":" in scrap_stat else 0
                        }
                else: res = await process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map, cache_manager)
                
                if "tokens" in res:
                    is_success = res.get("type") == "success"
                    await r_q.put({'type': 'tokens', 'in': res["tokens"]["in"], 'out': res["tokens"]["out"], 'think': res["tokens"].get("think", 0), 'rows': res.get("llm_rows", 0) if is_success else 0, 'calls': res.get("llm_calls", 0) if is_success else 0})

                if res["type"] == "success":
                    if self.mode != "phase2":
                        r1, r2, r3 = h_map["r1"], h_map["r2"], h_map["r3"]
                        r3_end = "AA" if r3 == "Y" else "Z"
                        await r_q.put({'range': f"{r1}{idx}:{r2}{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld1"], res["ld2"], res["bmp1"], res["bmr1"], res["bmp2"], res["bmr2"], res["bm_name"], res["bm_id"], res["sf"], res["feed_id"]]]})
                        await r_q.put({'range': f"{r3}{idx}:{r3_end}{idx}", 'values': [[res["tokens"]["in"], res["tokens"]["out"], res["tokens"].get("think", 0)]]})
                    
                    if self.mode != "phase1":
                        pipeline_logger.info(f"PIPELINE: Updating Tracxn for {domain}")
                        tags = res["hashtags"] + ["bu_llm_sd_ld", "bu_Internal_SRprocess_TypeA"]
                        payload = {"id": res["dp_id"], "description": {"value": res["ld1"]}, "shortDescription": {"value": res["sd"]}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}}
                        s1, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, json_data=payload, headers=HEADERS)
                        edits = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                        
                        f_id = res["feed_id"]
                        if res["bm_id"] != "No ID":
                            s2, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/3.0/w/theme-company-association", tracxn_limiter, json_data={"object": {"themeId": f_id, "status": "PUBLISHED", "businessModelId": res["bm_id"], "companyId": res["dp_id"]}, "opType": "Update"}, headers=HEADERS)
                            bm_up = "Done" if s2 in (200, 201) else ("Duplicate/Already Moved" if s2 == 422 else ("Funnel State Conflicts" if s2 == 400 else str(s2)))
                        else:
                            bm_up = "NotUpdated"
                        
                        f_id_to_move = "5dc5863a2799a51cc0ff30e2" # Moved to Published
                        As, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/force-assign", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "sourceDetails": {"source": "Write API"}, "comment": "This is done by Write API"}, headers=HEADERS)
                        if As in (200, 201):
                            ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": [f_id_to_move], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                        else:
                            ms = "Assign Failed"
                        fun = "Done" if ms in (200, 201) else ("Assign Failed" if ms == "Assign Failed" else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        
                        # Find Column U/V mapping
                        if h_map["r1"] == "J":
                            u_col, w_col = "V", "X"
                        else:
                            u_col, w_col = "U", "W"
                        await r_q.put({'range': f"{u_col}{idx}:{w_col}{idx}", 'values': [[edits, bm_up, fun]]})
                        await r_q.put({'type': 'progress', 'is_success': edits in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts")})
                    else: await r_q.put({'type': 'progress', 'is_success': True})
                else:
                    reason = res.get('reason', 'Failed')
                    pipeline_logger.error(f"PIPELINE FAILED: {domain} | {reason}")
                    stat_col = h_map["r1"]
                    await r_q.put({'range': f"{stat_col}{idx}", 'values': [[reason]]})
                    await r_q.put({'type': 'progress', 'is_success': False})
            except Exception as e:
                pipeline_logger.error(f"FATAL WORKER ERROR for {domain if domain else 'Unknown'}: {e}")
                await r_q.put({'type': 'progress', 'is_success': False})
            finally: w_q.task_done()

    async def sheet_writer(self, r_q, ws, total, gc, pipeline_name):
        processed, s, f = set(), 0, 0
        batch_in, batch_out, batch_think, batch_rows, batch_calls = 0, 0, 0, 0, 0
        while True:
            updates = []
            while not r_q.empty() and len(updates) < CONFIG["BATCH_SIZE"]:
                item = await r_q.get()
                if isinstance(item, dict):
                    if item.get('type') == 'progress':
                        if item.get('is_success'): s += 1
                        else: f += 1
                        r_q.task_done(); continue
                    if item.get('type') == 'tokens':
                        batch_in += item.get('in', 0)
                        batch_out += item.get('out', 0)
                        batch_think += item.get('think', 0)
                        batch_rows += item.get('rows', 0)
                        batch_calls += item.get('calls', 0)
                        r_q.task_done(); continue
                updates.append(item)
            if updates:
                try:
                    await ws.batch_update(updates, value_input_option='USER_ENTERED')
                    for u in updates:
                        m = re.search(r'\d+', u['range'])
                        if m: processed.add(int(m.group()))
                    
                    if batch_in > 0 or batch_out > 0 or batch_think > 0 or batch_rows > 0:
                        try:
                            t_sheet = await gc.open_by_key(CONFIG["TRACKING_SHEET_ID"])
                            t_ws = await t_sheet.worksheet(pipeline_name)
                            vals = await t_ws.batch_get(["B2", "B3", "B4", "B5", "B6"])
                            curr_in = int(vals[0][0][0]) if vals and vals[0] and vals[0][0] else 0
                            curr_out = int(vals[1][0][0]) if len(vals) > 1 and vals[1] and vals[1][0] else 0
                            curr_think = int(vals[2][0][0]) if len(vals) > 2 and vals[2] and vals[2][0] else 0
                            curr_rows = int(vals[3][0][0]) if len(vals) > 3 and vals[3] and vals[3][0] else 0
                            curr_calls = int(vals[4][0][0]) if len(vals) > 4 and vals[4] and vals[4][0] else 0
                            await t_ws.batch_update([
                                {'range': 'B2', 'values': [[curr_in + batch_in]]},
                                {'range': 'B3', 'values': [[curr_out + batch_out]]},
                                {'range': 'B4', 'values': [[curr_think + batch_think]]},
                                {'range': 'B5', 'values': [[curr_rows + batch_rows]]},
                                {'range': 'B6', 'values': [[curr_calls + batch_calls]]}
                            ], value_input_option='USER_ENTERED')
                            batch_in, batch_out, batch_think, batch_rows, batch_calls = 0, 0, 0, 0, 0
                        except Exception as e:
                            logging.error(f"Error updating tracking sheet: {e}")
                except Exception as e:
                    logging.error(f"Error while calling batch_update: {e}")
                finally:
                    for _ in updates: r_q.task_done()
                    current_completed = s + f
                    self.report_progress(current_completed, total, s, f)
            else: 
                current_completed = s + f
                self.report_progress(current_completed, total, s, f)
            await asyncio.sleep(1)

    def report_progress(self, curr, total, s, f):
        try:
            with open(".progress.json", "w") as file: json.dump({"current": curr, "total": total, "success": s, "fail": f}, file)
        except: pass

async def main():
    try:
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
            
        pipeline = TypeAPipeline(row, mode)
        if sheet_id:
            pipeline.config["SHEET_ID"] = sheet_id
        pipeline.apply_formatting = apply_formatting
        await pipeline.run()
    except Exception as e:
        import traceback
        pipeline_logger.critical(f"FATAL PIPELINE CRASH: {e}\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__": asyncio.run(main())
