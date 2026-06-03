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
    "SHEET_ID": settings.TYPEC_SHEET_ID,
    "PROMPTS_SHEET_ID": settings.PROMPTS_SHEET_ID,
    "FEED_OWNER_SHEET_ID": settings.FEED_OWNER_SHEET_ID,
    "EXTRACTING_SHEET_NAME": "DB",
    "TRACKING_SHEET_ID": "1OvBOAXc_Y5aDLcK-BGCALFUZyJWLYolmFkr3tmo7mj4",
    "CREDENTIALS_FILE": os.path.join(os.path.dirname(os.path.abspath(__file__)), "TypeC.json"),
    "MAX_WORKERS": _DYNAMIC_WORKERS,
    "MAX_CONCURRENT_BROWSERS": _DYNAMIC_WORKERS,
    "GEMINI_API_URL": settings.GEMINI_API_URL,
    "GEMINI_API_KEY": settings.TYPEC_GEMINI_API_KEY,
    "MAX_PROMPT_SIZE": settings.MAX_PROMPT_SIZE,
    "BATCH_SIZE": settings.BATCH_SIZE,
    "REQUEST_TIMEOUT": settings.REQUEST_TIMEOUT,
    "MAX_RETRIES": settings.MAX_RETRIES,
    "RETRY_DELAY": settings.RETRY_DELAY
}

HEADERS = {
    "accessToken": settings.TYPEC_TRACXN_ACCESS_TOKEN,
    "Content-Type": "application/json",
    "X-Request-Source": 'Type-C-Publishing'
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

async def process_domain_stage1(browser, session, row, prompts, f_ids, h_map, cache_manager) -> Dict:
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
            pipeline_logger.warning(f"PROCESS FAILED HTTPS for {domain}. Retrying with HTTP...")
            html, final_url, reason = await fetcher.fetch(browser, f"http://{domain}")

        if html is None:
            pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: {reason}")
            return {"type": "error", "reason": reason}
        if len(html) < 300:
            pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: Low Content ({len(html)} chars)")
            return {"type": "error", "reason": "Low Content"}
            
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
        
    llm_calls = 0
    llm_rows = 1
    
    parts_p1 = prompts[0].split("XX")
    if len(parts_p1) == 2:
        sys_p1 = parts_p1[0].strip() + "\n\n[DATA PROVIDED BY USER BELOW]\n\n" + parts_p1[1].strip()
        user_p1 = "URL: " + str(final_url) + "\n\nRaw Content:\n" + body[:20000]
        cache_id = await cache_manager.get_or_create(session, "prompt_0", sys_p1)
        res_obj = await call_gemini_api(session, user_p1, gemini_limiter, system_instruction=sys_p1, cached_content_name=cache_id)
    else:
        p1 = prompts[0].replace("XX", body[:20000])
        res_obj = await call_gemini_api(session, p1, gemini_limiter)
    
    llm_calls += 1
    res, in_p, out_p = res_obj.text, res_obj.prompt_tokens, res_obj.candidate_tokens
    think_tokens = res_obj.thinking_tokens
    think_text = res_obj.thinking_text
    
    tokens = {"in": in_p, "out": out_p, "think": think_tokens}
    
    sd, ld = extract_descriptions(res)
    if sd == "NO_DATA":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Insufficient content (AI reported NO_DATA)")
        return {"type": "error", "reason": "Low content", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows}
    if sd == "PARKED_LLM":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked (AI reported PARKED_LLM)")
        return {"type": "error", "reason": "Parked", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows}
    
    if not sd or not ld: 
        pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: LLM failed to generate descriptions")
        return {"type": "error", "reason": "LLM failed", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows}

    pipeline_logger.info(f"PROCESS SUCCESS: {domain}")
    return {
        "type": "success", "sd": sd, "ld": ld[:40000], "tokens": tokens, "thinking_text": think_text,
        "llm_calls": llm_calls, "llm_rows": llm_rows,
        "dp_id": row[h_map["dp_id"]], "funnel_id": row[h_map["funnel_id"]], 
        "funnel_name": row[h_map["funnel_name"]], "company_name": row[h_map["company_name"]],
        "tags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
        "body_len": len(body)
    }

class TypeCPipeline:
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
                # Shifted mapping
                h_map = {
                    "domain": 2, "dp_id": 3, "funnel_name": 4, "funnel_id": 5, "tags": 6, "company_name": 7,
                    "skip": 8, "scrap_stat": 8, "sd": 9, "ld": 10, "feed_id": 11,
                    "r1": "I", "r2": "N", "r3": "P", # Shifted: I-N, P-R
                    "raw_data": 17 # Col R
                }
                pipeline_logger.info("Detected SHIFTED column mapping (Index 2 for Domain)")
            else:
                # Standard mapping
                h_map = {
                    "domain": 1, "dp_id": 2, "funnel_name": 3, "funnel_id": 4, "tags": 5, "company_name": 6,
                    "skip": 7, "scrap_stat": 7, "sd": 8, "ld": 9, "feed_id": 10,
                    "r1": "H", "r2": "M", "r3": "O", # Standard: H-M, O-Q
                    "raw_data": 17 # Col R
                }
                pipeline_logger.info("Detected STANDARD column mapping (Index 1 for Domain)")
        else:
            h_map = {}
        
        p_sheet = await gc.open_by_key(self.config["PROMPTS_SHEET_ID"])
        prompts = [r[1] for r in (await (await p_sheet.worksheet("Prompts")).get_all_values())[1:10]]
        fo_sheet = await gc.open_by_key(self.config["FEED_OWNER_SHEET_ID"])
        f_ids = {r[0]: r[1] for r in (await (await fo_sheet.worksheet("Feed Owner Details")).get_all_values())}

        work_queue, result_queue = asyncio.Queue(), asyncio.Queue()
        for idx, row in data_rows:
            await work_queue.put((idx, row))

        cache_manager = GeminiCacheManager(settings.TYPEC_GEMINI_API_KEY)
        async with aiohttp.ClientSession() as session:
            if self.mode == "phase2":
                tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, None, session, prompts, f_ids, h_map, cache_manager)) for _ in range(CONFIG["MAX_WORKERS"])]
                writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows), gc, "TypeC"))
                await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()
            else:
                while True:
                    try:
                        # Ensure system is healthy before launching/relaunching browser
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
                            tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, browser, session, prompts, f_ids, h_map, cache_manager)) for _ in range(CONFIG["MAX_WORKERS"])]
                            writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows), gc, "TypeC"))
                            
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
                        pipeline_logger.error(f"BROWSER ENGINE CRASHED (Leak Recovery): {e}. Waiting for resources to restart...")
                        await asyncio.sleep(5)
                        await SystemHealthMonitor(cpu_threshold=80, mem_threshold=85).wait_for_resources(logger=pipeline_logger)

    async def domain_worker(self, w_q, r_q, browser, session, prompts, f_ids, h_map, cache_manager):
        monitor = SystemHealthMonitor()
        import random
        # Jitter start to prevent CPU storm
        j_time = random.uniform(1.0, 5.0)
        pipeline_logger.debug(f"WORKER: Jittering for {j_time:.1f}s...")
        await asyncio.sleep(j_time)
        while True:
            idx, row = await w_q.get()
            try:
                # Initial resource check before starting a new row
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
                            "type": "success", "sd": sd, "ld": ld, "dp_id": dp_id, "funnel_id": funnel_id,
                            "funnel_name": row[h_map["funnel_name"]] if len(row) > h_map["funnel_name"] else "", 
                            "company_name": row[h_map["company_name"]] if len(row) > h_map["company_name"] else "",
                            "feed_id": row[r1_idx + 3] if len(row) > r1_idx + 3 else "",
                            "tags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
                            "tokens": {"in":0, "out":0, "think":0}, "body_len": int(scrap_stat.split(":")[-1]) if ":" in scrap_stat else 0
                        }
                else: res = await process_domain_stage1(browser, session, row, prompts, f_ids, h_map, cache_manager)
                
                if "tokens" in res:
                    is_success = res.get("type") == "success"
                    await r_q.put({'type': 'tokens', 'in': res["tokens"]["in"], 'out': res["tokens"]["out"], 'think': res["tokens"].get("think", 0), 'rows': res.get("llm_rows", 0) if is_success else 0, 'calls': res.get("llm_calls", 0) if is_success else 0})
                
                if res["type"] == "success":
                    r1, r2, r3 = h_map["r1"], h_map["r2"], h_map["r3"]
                    if self.mode != "phase2":
                        r3_end = "P" if r3 == "N" else "O" # wait, O:Q (3) or N:P (3)
                        r2_letter = chr(ord(r1) + 2) # H->J, I->K
                        await r_q.put({'range': f"{r1}{idx}:{r2_letter}{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld"]]]})
                        
                        r3_end_letter = chr(ord(r3) + 2) # O->Q, N->P? No, N->P is index 13-15?
                        # O is 14, Q is 16. (3 columns)
                        # N is 13, P is 15. (3 columns)
                        await r_q.put({'range': f"{r3}{idx}:{r3_end_letter}{idx}", 'values': [[res["tokens"]["in"], res["tokens"]["out"], res["tokens"].get("think", 0)]]})
                        
                        if self.mode == "phase1":
                            funnel_name = res.get("funnel_name") or ""
                            feed = funnel_name.split(" : ")[1] if " : " in funnel_name else funnel_name
                            feed_id = f_ids.get(feed, "")
                            k_col = chr(ord(r1) + 3)
                            await r_q.put({'range': f"{k_col}{idx}", 'values': [[feed_id]]})
                    
                    if self.mode != "phase1":
                        pipeline_logger.info(f"PIPELINE: Updating Tracxn for {domain}")
                        tags = res["tags"] + ["bu_llm_typec_autopublish"]
                        funnel_name = res.get("funnel_name") or ""
                        feed = funnel_name.split(" : ")[1] if " : " in funnel_name else funnel_name
                        feed_id = f_ids.get(feed, "")
                        
                        # 1. Fetch edit history to detect publish.edits@tracxn.com edits for foundedYear & companyLocation
                        data_map = {"foundedYear": "No", "companyLocation": "No"}
                        try:
                            eh_status, eh_res = await call_tracxn_api(
                                session, 
                                f"https://platform.tracxn.com/data/edithistory/edits/DOMAIN_PROFILE/{res['dp_id']}", 
                                tracxn_limiter, 
                                method="get", 
                                headers=HEADERS
                            )
                            if eh_status == 200 and isinstance(eh_res, list):
                                for item in eh_res:
                                    a_name = item.get("attributeName")
                                    cred_by = item.get("createdBy")
                                    if a_name in data_map:
                                        data_map[a_name] = cred_by
                        except Exception as eh_err:
                            pipeline_logger.error(f"Failed to fetch edit history for {domain}: {eh_err}")
                        
                        # 2. Build the domain-profile update payload with PUBLISHED status
                        dp_payload = {
                            "id": res["dp_id"],
                            "companyName": {"value": res["company_name"]},
                            "description": {"value": res["ld"]},
                            "shortDescription": {"value": res["sd"]},
                            "keywords": {"value": {"HASHTAGS": tags}},
                            "publishingDepth": {"value": "Pub 2 - Partial"},
                            "status": {"value": "PUBLISHED"}
                        }
                        
                        # Apply Bot Cleanup: Clear if authored by publish.edits@tracxn.com
                        if data_map.get("foundedYear") == "publish.edits@tracxn.com":
                            dp_payload["foundedYear"] = {"value": None}
                            pipeline_logger.info(f"BOT CLEANUP: Clearing foundedYear for {domain}")
                        if data_map.get("companyLocation") == "publish.edits@tracxn.com":
                            dp_payload["companyLocation"] = {"value": None}
                            pipeline_logger.info(f"BOT CLEANUP: Clearing companyLocation for {domain}")
                            
                        f_stat, sdld, fun = "N/A", "N/A", "N/A"
                        async def update_dp():
                            return await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data=dp_payload, headers=HEADERS)
                            
                        async def update_bm():
                            if feed_id:
                                return await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/3.0/w/theme-company-association", tracxn_limiter, method="put", json_data={"object": {"themeId": feed_id, "status": "PUBLISHED", "companyId": res["dp_id"]}, "opType": "Update"}, headers=HEADERS)
                            return 200, None
                            
                        async def update_funnel():
                            f_id_to_move = "5dc5863a2799a51cc0ff30e2" if feed_id else "591d37b884ae06633a652496"
                            As, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/force-assign", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "sourceDetails": {"source": "Write API"}, "comment": "This is done by Write API"}, headers=HEADERS)
                            if As in (200, 201):
                                ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": [f_id_to_move], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                                return ms
                            return "Assign Failed"
                            
                        (s1, _), (s_f, _), ms = await asyncio.gather(update_dp(), update_bm(), update_funnel())
                        
                        sdld = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                        if feed_id:
                            f_stat = "Done" if s_f in (200, 201) else ("Duplicate/Already Moved" if s_f == 422 else ("Funnel State Conflicts" if s_f == 400 else str(s_f)))
                            fun = "Done" if ms in (200, 201) else ("Assign Failed" if ms == "Assign Failed" else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        else:
                            fun = "Sent discovery" if ms in (200, 201) else ("Assign Failed" if ms == "Assign Failed" else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        
                        # Dynamic Tracxn status columns based on results start column r1
                        k_col = chr(ord(r1) + 3)
                        n_col = chr(ord(r1) + 6)
                        await r_q.put({'range': f"{k_col}{idx}:{n_col}{idx}", 'values': [[feed_id, sdld, f_stat, fun]]})
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
                pipeline_logger.error(f"FATAL WORKER ERROR for {row[h_map['domain']] if row else 'Unknown'}: {e}")
                await r_q.put({'type': 'progress', 'is_success': False})
            finally:
                w_q.task_done()

    async def sheet_writer(self, r_q, ws, total, gc, pipeline_name):
        processed_indices, success_count, fail_count = set(), 0, 0
        batch_in, batch_out, batch_think, batch_rows, batch_calls = 0, 0, 0, 0, 0
        while True:
            updates = []
            while not r_q.empty() and len(updates) < CONFIG["BATCH_SIZE"]:
                item = await r_q.get()
                if isinstance(item, dict):
                    if item.get('type') == 'progress':
                        if item.get('is_success'): success_count += 1
                        else: fail_count += 1
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
                        match = re.search(r'\d+', u['range'])
                        if match: processed_indices.add(int(match.group()))
                    
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
                            pipeline_logger.error(f"TRACKING SHEET ERR: {e}")
                except Exception as e:
                    pipeline_logger.error(f"SHEET WRITER ERR: {e}")
                finally:
                    for _ in updates: r_q.task_done()
                    current_completed = success_count + fail_count
                    self.report_progress(current_completed, total, success_count, fail_count)
                    pipeline_logger.info(f"PROGRESS: {current_completed}/{total} | Success: {success_count} | Fail: {fail_count}")
            else: 
                current_completed = success_count + fail_count
                self.report_progress(current_completed, total, success_count, fail_count)
            await asyncio.sleep(1)

    def report_progress(self, curr, total, success, fail):
        try:
            with open(".progress.json", "w") as f: json.dump({"current": curr, "total": total, "success": success, "fail": fail}, f)
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
            
        pipeline = TypeCPipeline(row, mode)
        if sheet_id:
            pipeline.config["SHEET_ID"] = sheet_id
        pipeline.apply_formatting = apply_formatting
        await pipeline.run()
    except Exception as e:
        import traceback
        pipeline_logger.critical(f"FATAL PIPELINE CRASH: {e}\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__": asyncio.run(main())
