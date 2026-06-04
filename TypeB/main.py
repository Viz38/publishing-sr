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
from gspread_formatting import set_frozen
from typing import Dict, List, Set, Tuple, Optional, Union, Any
from urllib.parse import urlparse
from dotenv import load_dotenv

from sr_common.config import settings
from sr_common.utils import (
    call_gemini_api, call_tracxn_api, clean_html, extract_descriptions, 
    is_parked_domain, get_dynamic_max_workers, SystemHealthMonitor, 
    GeminiCacheManager
)
from sr_common.clients import RateLimiter, MultiTierRateLimiter, GoogleSheetsClient
from sr_common.fetcher import StealthFetcher
from sr_common.stealth import get_browser_profile

_DYNAMIC_WORKERS = get_dynamic_max_workers()

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
    "TRACKING_SHEET_ID": "1OvBOAXc_Y5aDLcK-BGCALFUZyJWLYolmFkr3tmo7mj4",
    "CREDENTIALS_FILE": os.path.join(os.path.dirname(os.path.abspath(__file__)), "TypeB.json"),
    "MAX_WORKERS": _DYNAMIC_WORKERS,
    "MAX_CONCURRENT_BROWSERS": _DYNAMIC_WORKERS,
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
tracxn_limiter = MultiTierRateLimiter(os.path.join(LOGS_DIR, 'tracxn_rate_limit.db'), {'second': 100, 'minute': 1000, 'hour': 10000, 'day': 100000})

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

# Global fetcher instance
fetcher = StealthFetcher()

async def process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map, cache_manager) -> Dict:
    domain = row[h_map["domain"]]
    pipeline_logger.info(f"PROCESS START: {domain}")
    
    raw_data_col = h_map.get("raw_data")
    raw_data = row[raw_data_col] if raw_data_col is not None and len(row) > raw_data_col else ""
    
    if raw_data.strip():
        pipeline_logger.info(f"PROCESS: Found raw data for {domain}, skipping scrape.")
        final_url = f"https://{domain}"
        body = raw_data.strip()
    else:
        html, final_url, reason = await fetcher.fetch(browser, f"https://{domain}")

        if html is None:
            pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: {reason}")
            return {"type": "error", "reason": reason}
        if len(html) < 300:
            pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: Low Content ({len(html)} chars)")
            return {"type": "error", "reason": "Low Content"}
            
        body = await clean_html(html)
        pipeline_logger.info(f"PROCESS: Scraped {domain} | Length: {len(body)}")
        
        # Check for parked
        parked, kw = is_parked_domain(html, body)
        if parked:
            pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked ({kw})")
            return {"type": "error", "reason": "Parked"}
    
    if len(body) < 100: 
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Low content")
        return {"type": "error", "reason": "Low content"}
        
    llm_calls = 0
    llm_rows = 1
    
    parts_p1 = prompts[0].split("XX")
    if len(parts_p1) == 2:
        sys_p1 = parts_p1[0].strip() + "\n\n[DATA PROVIDED BY USER BELOW]\n\n" + parts_p1[1].strip()
        user_p1 = "URL: " + str(final_url) + "\n\nRaw Content:\n" + body
        cache_id = await cache_manager.get_or_create(session, "prompt_0", sys_p1)
        res_p1_obj = await call_gemini_api(session, user_p1, gemini_limiter, system_instruction=sys_p1, cached_content_name=cache_id)
    else:
        p1 = prompts[0].replace("XX", body[:40000])
        res_p1_obj = await call_gemini_api(session, p1, gemini_limiter)
    
    llm_calls += 1
    res_p1 = res_p1_obj.text
    in1, out1, think1 = res_p1_obj.prompt_tokens, res_p1_obj.candidate_tokens, res_p1_obj.thinking_tokens
    think_text = f"P1:\n{res_p1_obj.thinking_text}\n" if res_p1_obj.thinking_text else ""
    
    tokens = {"in": in1, "out": out1, "think": think1}
    
    sd, ld = extract_descriptions(res_p1)
    if sd == "NO_DATA":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Insufficient content (AI reported NO_DATA)")
        return {"type": "error", "reason": "Low content", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows}
    if sd == "PARKED_LLM":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked (AI reported PARKED_LLM)")
        return {"type": "error", "reason": "Parked", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows}
    
    if not sd or not ld: 
        pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: LLM failed to generate descriptions")
        return {"type": "error", "reason": "LLM failed", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows}
    
    feed = row[h_map["feed"]].split(" : ")[1] if " : " in row[h_map["feed"]] else row[h_map["feed"]]
    f_id, f_def = f_ids.get(feed, ""), f_defs.get(feed, "")
    
    pipeline_logger.info(f"PROCESS: Running BM prediction for {domain}")
    bm_res, bm_name, bm_id, f_chk, in2, out2, think2 = "", "", None, "No", 0, 0, 0
    if feed in bm_paths:
        bm_paths_str = "\n".join([" ".join(map(str, r)) for r in bm_paths[feed]["data"]])
        parts_bm = prompts[3].split("XX")
        if len(parts_bm) == 2:
            sys_bm = (parts_bm[0].strip() + "\n\n[COMPANY DESCRIPTION PROVIDED BY USER BELOW]\n\n" + parts_bm[1].strip()).replace("BMPathstr", bm_paths_str).replace("YY", f_def)
            user_bm = "Company Description:\n" + ld
            cache_key = f"prompt_3_{feed.replace(' ', '_')}"
            cache_id = await cache_manager.get_or_create(session, cache_key, sys_bm)
            res_bm_obj = await call_gemini_api(session, user_bm, gemini_limiter, system_instruction=sys_bm, cached_content_name=cache_id)
        else:
            bm_p = prompts[3].replace("XX", ld).replace("BMPathstr", bm_paths_str).replace("YY", f_def)
            res_bm_obj = await call_gemini_api(session, bm_p, gemini_limiter)
            
        llm_calls += 1
        bm_res, in2, out2, think2 = res_bm_obj.text, res_bm_obj.prompt_tokens, res_bm_obj.candidate_tokens, res_bm_obj.thinking_tokens
        if res_bm_obj.thinking_text: think_text += f"BM:\n{res_bm_obj.thinking_text}\n"
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
        "tokens": {"in": in1+in2, "out": out1+out2, "think": think1+think2}, "think_text": think_text,
        "llm_calls": llm_calls, "llm_rows": llm_rows,
        "feed_id": f_id, "body_len": len(body)
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
                # Shifted mapping (Columns shifted right by 1)
                h_map = {
                    "domain": 2, "dp_id": 3, "feed": 4, "funnel_id": 5, "tags": 6,
                    "skip": 8, "scrap_stat": 9, "sd": 10, "ld": 11, "feed_id": 21,
                    "r1": "J", "r2": "T", "r3": "U", # Shifted: J-T, U-W
                    "raw_data": 22 # Col W
                }
                pipeline_logger.info("Detected SHIFTED column mapping (Index 2 for Domain)")
            else:
                # Standard mapping
                h_map = {
                    "domain": 1, "dp_id": 2, "feed": 3, "funnel_id": 4, "tags": 5,
                    "skip": 6, "scrap_stat": 7, "sd": 8, "ld": 9, "feed_id": 15,
                    "r1": "H", "r2": "S", "r3": "T", # Standard: H-S, T-V
                    "raw_data": 22 # Col W
                }
                pipeline_logger.info("Detected STANDARD column mapping (Index 1 for Domain)")
        else:
            h_map = {}
        
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
        for idx, row in data_rows:
            if len(row) > h_map["skip"] and row[h_map["skip"]] == "Yes": continue
            await work_queue.put((idx, row))

        cache_manager = GeminiCacheManager(settings.TYPEB_GEMINI_API_KEY)
        async with aiohttp.ClientSession() as session:
            if self.mode == "phase2":
                tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, None, session, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map, cache_manager)) for _ in range(CONFIG["MAX_WORKERS"])]
                writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows), gc, "TypeB"))
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
                            tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, browser, session, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map, cache_manager)) for _ in range(CONFIG["MAX_WORKERS"])]
                            writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows), gc, "TypeB"))
                            
                            queue_task = asyncio.create_task(work_queue.join())
                            done, pending = await asyncio.wait([queue_task] + tasks, return_when=asyncio.FIRST_COMPLETED)
                            
                            if queue_task in done:
                                [t.cancel() for t in tasks]
                                await result_queue.join()
                                writer_task.cancel()
                                break
                            else:
                                for t in done:
                                    if t != queue_task and t.exception():
                                        raise t.exception()
                    except Exception as e:
                        pipeline_logger.error(f"BROWSER ENGINE CRASHED (Leak Recovery): {e}. Restarting browser...")
                        await asyncio.sleep(5)
        await SystemHealthMonitor(cpu_threshold=80, mem_threshold=85).wait_for_resources(logger=pipeline_logger)

    async def domain_worker(self, w_q, r_q, browser, session, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map, cache_manager):
        monitor = SystemHealthMonitor()
        import random
        # Jitter start to prevent CPU storm
        await asyncio.sleep(random.uniform(0.5, 3.0))
        while True:
            idx, row = await w_q.get()
            try:
                await monitor.wait_for_resources(logger=pipeline_logger, timeout=300)
                domain = row[h_map["domain"]]
                date_str = datetime.now().strftime("%d-%b-%Y")
                await r_q.put({'range': f"A{idx}", 'values': [[date_str]]})
                if self.mode == "phase2":
                    sd, ld, dp_id, funnel_id = row[h_map["sd"]], row[h_map["ld"]], row[h_map["dp_id"]], row[h_map["funnel_id"]]
                    r1_idx = ord(h_map["r1"]) - ord('A')
                    scrap_stat = row[r1_idx] if len(row) > r1_idx else ""
                    if not (sd and ld and dp_id and funnel_id and scrap_stat.startswith("Yes")):
                        res = {"type": "failed", "reason": "Missing Phase 1 inputs"}
                    else:
                        res = {
                            "type": "success", "dp_id": dp_id, "funnel_id": funnel_id, "sd": sd, "ld": ld,
                            "feedcheck": row[r1_idx + 3] if len(row) > r1_idx + 3 else "", 
                            "bm_res": row[r1_idx + 4] if len(row) > r1_idx + 4 else "", 
                            "bm_name": row[r1_idx + 5] if len(row) > r1_idx + 5 else "", 
                            "bm_id": row[r1_idx + 6] if len(row) > r1_idx + 6 else "",
                            "hash_status": row[r1_idx + 7] if len(row) > r1_idx + 7 else "N/A",
                            "feed_id": row[r1_idx + 8] if len(row) > r1_idx + 8 else "", 
                            "hashtags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
                            "tokens": {"in":0, "out":0, "think":0}, "body_len": int(scrap_stat.split(":")[-1]) if ":" in scrap_stat else 0
                        }
                else: res = await process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map, cache_manager)
                
                if "tokens" in res:
                    is_success = res.get("type") == "success"
                    await r_q.put({'type': 'tokens', 'in': res["tokens"]["in"], 'out': res["tokens"]["out"], 'think': res["tokens"].get("think", 0), 'rows': res.get("llm_rows", 0) if is_success else 0, 'calls': res.get("llm_calls", 0) if is_success else 0})

                if res["type"] == "success":
                    hash_stat = res.get("hash_status") or ("Yes" if res.get("hash_removed") else "No")
                    if self.mode != "phase2":
                        r1, r2, r3 = h_map["r1"], h_map["r2"], h_map["r3"]
                        r3_end = "W" if r3 == "U" else "V"
                        
                        await r_q.put({'range': f"{r1}{idx}:{r2}{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld"], res["feedcheck"], res["bm_res"], res["bm_name"], res["bm_id"], hash_stat, res["feed_id"]]]})
                        await r_q.put({'range': f"{r3}{idx}:{r3_end}{idx}", 'values': [[res["tokens"]["in"], res["tokens"]["out"], res["tokens"].get("think", 0)]]})
                    
                    if self.mode != "phase1":
                        pipeline_logger.info(f"PIPELINE: Updating Tracxn for {domain}")
                        tags = res["hashtags"] + ["bu_llm_sd_ld", "llmbasedpublishing"]
                        if res["feedcheck"] == "Yes": tags.append("bu_llm_businessmodel_prediction")
                        sdld, bm, fun = "N/A", "N/A", "N/A"
                        f_id = res["feed_id"] or f_ids.get(res["bm_name"]) or f_ids.get(row[3] if len(row) > 3 else "")
                        async def update_dp():
                            return await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data={"id": res["dp_id"], "description": {"value": res["ld"]}, "shortDescription": {"value": res["sd"]}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}}, headers=HEADERS)
                            
                        async def update_bm():
                            if res["feedcheck"] == "Yes" and res["bm_id"]:
                                return await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/3.0/w/theme-company-association", tracxn_limiter, method="put", json_data={"object": {"themeId": f_id, "status": "PUBLISHED", "businessModelId": res["bm_id"], "companyId": res["dp_id"]}, "opType": "Update"}, headers=HEADERS)
                            return 200, None
                            
                        async def update_funnel():
                            if res.get("feedcheck") == "No":
                                f_id_to_move = "64197f01a6dcff6572453ead"
                            else:
                                f_id_to_move = "5dc5863a2799a51cc0ff30e2" if (res.get("feedcheck") == "Yes" and res.get("bm_id")) else "591d37b884ae06633a652496"
                                
                            As, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/force-assign", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "sourceDetails": {"source": "Write API"}, "comment": "This is done by Write API"}, headers=HEADERS)
                            if As in (200, 201):
                                ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": [f_id_to_move], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                                if ms == 400 and f_id_to_move != "64197f01a6dcff6572453ead":
                                    ms2, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": ["64197f01a6dcff6572453ead"], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                                    return ms2
                                return ms
                            return "Assign Failed"
                            
                        (s1, _), (s2, _), ms = await asyncio.gather(update_dp(), update_bm(), update_funnel())
                        
                        sdld = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                        if res["feedcheck"] == "Yes" and res["bm_id"]:
                            bm = "Done" if s2 in (200, 201) else ("Duplicate/Already Moved" if s2 == 422 else ("Funnel State Conflicts" if s2 == 400 else str(s2)))
                            fun = "Done" if ms in (200, 201) else ("Assign Failed" if ms == "Assign Failed" else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        else:
                            fun = "Sent discovery" if ms in (200, 201) else ("Assign Failed" if ms == "Assign Failed" else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        
                        if h_map["r1"] == "J":
                            o_col, s_col = "P", "T"
                        else:
                            o_col, s_col = "O", "S"
                        await r_q.put({'range': f"{o_col}{idx}:{s_col}{idx}", 'values': [[hash_stat, f_id, sdld, bm, fun]]})
                        await r_q.put({'type': 'progress', 'is_success': sdld in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts")})
                    else: await r_q.put({'type': 'progress', 'is_success': True})
                else:
                    reason = res.get('reason', 'Failed')
                    pipeline_logger.error(f"PIPELINE FAILED: {domain} | {reason}")
                    stat_col = h_map["r1"]
                    await r_q.put({'range': f"{stat_col}{idx}", 'values': [[reason]]})
                    await r_q.put({'type': 'progress', 'is_success': False})
            except Exception as e:
                if "Resource saturation" in str(e):
                    pipeline_logger.warning(f"Re-queuing row {idx} due to Resource Saturation.")
                    await w_q.put((idx, row))
                    raise
                pipeline_logger.error(f"FATAL WORKER ERROR for {domain if domain else 'Unknown'}: {e}")
                await r_q.put({'type': 'progress', 'is_success': False})
            finally:
                w_q.task_done()

    async def sheet_writer(self, r_q, ws, total, gc, pipeline_name):
        processed_indices, success, fail = set(), 0, 0
        batch_in, batch_out, batch_think, batch_rows, batch_calls = 0, 0, 0, 0, 0
        updates = []
        last_flush = time.time()
        while True:
            try:
                # Wait up to 1.0s for an item to arrive in the queue
                item = await asyncio.wait_for(r_q.get(), timeout=1.0)
                
                if isinstance(item, dict):
                    if item.get('type') == 'progress':
                        if item.get('is_success'): success += 1
                        else: fail += 1
                        r_q.task_done()
                    elif item.get('type') == 'tokens':
                        batch_in += item.get('in', 0)
                        batch_out += item.get('out', 0)
                        batch_think += item.get('think', 0)
                        batch_rows += item.get('rows', 0)
                        batch_calls += item.get('calls', 0)
                        r_q.task_done()
                    else:
                        updates.append(item)
                
                # Fetch any additional items immediately available
                while not r_q.empty() and len(updates) < 100:
                    item = r_q.get_nowait()
                    if isinstance(item, dict):
                        if item.get('type') == 'progress':
                            if item.get('is_success'): success += 1
                            else: fail += 1
                            r_q.task_done()
                            continue
                        elif item.get('type') == 'tokens':
                            batch_in += item.get('in', 0)
                            batch_out += item.get('out', 0)
                            batch_think += item.get('think', 0)
                            batch_rows += item.get('rows', 0)
                            batch_calls += item.get('calls', 0)
                            r_q.task_done()
                            continue
                    updates.append(item)
            except asyncio.TimeoutError:
                pass
            
            if updates and (len(updates) >= 100 or time.time() - last_flush > 30 or (success + fail) == total):
                try:
                    # Sort updates by row number to ensure perfectly sequential Google Sheets writing
                    def get_row_num(u):
                        m = re.search(r'\d+', u.get('range', ''))
                        return int(m.group()) if m else 0
                    updates.sort(key=get_row_num)
                    
                    # CSV Buffer Backup
                    try:
                        import csv
                        csv_path = os.path.join(LOGS_DIR, 'results_backup.csv')
                        file_exists = os.path.exists(csv_path)
                        with open(csv_path, 'a', newline='', encoding='utf-8') as f:
                            writer = csv.writer(f)
                            if not file_exists:
                                writer.writerow(["Range", "Value1", "Value2", "Value3", "Value4", "Value5", "Value6", "Value7", "Value8"])
                            for u in updates:
                                vals = u.get('values', [[]])[0]
                                writer.writerow([u.get('range', '')] + [str(v)[:1000] for v in vals]) # Truncate long strings for CSV
                    except Exception as e:
                        pipeline_logger.error(f"CSV BACKUP ERR: {e}")

                    await ws.batch_update(updates, value_input_option='USER_ENTERED')
                    for u in updates:
                        match = re.search(r'\d+', u['range'])
                        if match: processed_indices.add(int(match.group()))
                    
                    if batch_in > 0 or batch_out > 0 or batch_think > 0 or batch_rows > 0:
                        async def _update_tracking(b_in, b_out, b_think, b_rows, b_calls):
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
                                    {'range': 'B2', 'values': [[curr_in + b_in]]},
                                    {'range': 'B3', 'values': [[curr_out + b_out]]},
                                    {'range': 'B4', 'values': [[curr_think + b_think]]},
                                    {'range': 'B5', 'values': [[curr_rows + b_rows]]},
                                    {'range': 'B6', 'values': [[curr_calls + b_calls]]}
                                ], value_input_option='USER_ENTERED')
                            except Exception as e:
                                pipeline_logger.error(f"TRACKING SHEET ERR: {e}")
                        
                        asyncio.create_task(_update_tracking(batch_in, batch_out, batch_think, batch_rows, batch_calls))
                        batch_in, batch_out, batch_think, batch_rows, batch_calls = 0, 0, 0, 0, 0
                except Exception as e:
                    pipeline_logger.error(f"SHEET WRITER ERR: {e}")
                finally:
                    for _ in updates: r_q.task_done()
                    updates = []
                    last_flush = time.time()
                    current_completed = success + fail
                    self.report_progress(current_completed, total, success, fail)
                    pipeline_logger.info(f"PROGRESS: {current_completed}/{total} | Success: {success} | Fail: {fail}")
            else: 
                current_completed = success + fail
                self.report_progress(current_completed, total, success, fail)

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
