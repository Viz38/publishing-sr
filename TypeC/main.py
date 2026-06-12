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
import time
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
    
    raw_data = row[17] if len(row) > 17 else ""
    
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
        res_obj = await call_gemini_api(session, user_p1, gemini_limiter, system_instruction=sys_p1, cached_content_name=cache_id)
    else:
        p1 = prompts[0].replace("XX", body[:40000])
        res_obj = await call_gemini_api(session, p1, gemini_limiter)
    
    llm_calls += 1
    res, in_p, out_p = res_obj.text, res_obj.prompt_tokens, res_obj.candidate_tokens
    think_tokens = res_obj.thinking_tokens
    think_text = res_obj.thinking_text
    
    tokens = {"in": in_p, "out": out_p, "think": think_tokens}
    
    sd, ld = extract_descriptions(res)
    if sd.startswith("REFUSAL:"):
        reason = sd.replace("REFUSAL:", "").strip()
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: LLM Refused - {reason}")
        return {"type": "error", "reason": f"LLM Refused - {reason}", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows}
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
                    r.extend([""] * max(0, 40 - len(r)))
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
        
        h_map = {
            "domain": 1, "dp_id": 2, "funnel_id": 4, "tags": 5, "company_name": 6,
            "sd": 8, "ld": 9, "feed_id": 10, "funnel_name": 3
        }
        
        all_rows = await ws.get_all_values()
        data_rows = [r for r in all_rows[self.start_row-1:] if len(r) > 1 and r[1].strip()]
        
        p_sheet = await gc.open_by_key(self.config["PROMPTS_SHEET_ID"])
        prompts = [r[1] for r in (await (await p_sheet.worksheet("Prompts")).get_all_values())[1:10]]
        fo_sheet = await gc.open_by_key(self.config["FEED_OWNER_SHEET_ID"])
        f_ids = {r[0]: r[1] for r in (await (await fo_sheet.worksheet("Feed Owner Details")).get_all_values())}

        work_queue, result_queue = asyncio.Queue(), asyncio.Queue()
        for idx, row in enumerate(data_rows, start=self.start_row):
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
        await asyncio.sleep(random.uniform(1.0, 5.0))
        while True:
            idx, row = await w_q.get()
            try:
                await monitor.wait_for_resources(logger=pipeline_logger, timeout=300)
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
                            "type": "success", "sd": sd, "ld": ld, "dp_id": dp_id, "funnel_id": funnel_id,
                            "funnel_name": row[h_map["funnel_name"]] if len(row) > h_map["funnel_name"] else "", 
                            "company_name": row[h_map["company_name"]] if len(row) > h_map["company_name"] else "",
                            "feed_id": row[h_map["feed_id"]] if len(row) > h_map["feed_id"] else "",
                            "tags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
                            "tokens": {"in":0, "out":0, "think":0}, "body_len": int(scrap_stat.split(":")[-1]) if ":" in scrap_stat else 0
                        }
                else: res = await process_domain_stage1(browser, session, row, prompts, f_ids, h_map, cache_manager)
                
                if "tokens" in res:
                    is_success = res.get("type") == "success"
                    await r_q.put({'type': 'tokens', 'in': res["tokens"]["in"], 'out': res["tokens"]["out"], 'think': res["tokens"].get("think", 0), 'rows': res.get("llm_rows", 0) if is_success else 0, 'calls': res.get("llm_calls", 0) if is_success else 0})
                
                if res["type"] == "success":
                    if self.mode != "phase2":
                        await r_q.put({'range': f"H{idx}:J{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld"]]]})
                        await r_q.put({'range': f"O{idx}:Q{idx}", 'values': [[res["tokens"]["in"], res["tokens"]["out"], res["tokens"].get("think", 0)]]})
                        if self.mode in ("phase1", "full"):
                            funnel_name = res.get("funnel_name") or ""
                            feed = funnel_name.split(" : ")[1] if " : " in funnel_name else funnel_name
                            feed_id = f_ids.get(feed, "")
                            res["feed_id"] = feed_id
                            await r_q.put({'range': f"K{idx}", 'values': [[feed_id]]})

                is_success = res["type"] == "success"
                is_full_success = is_success and res.get("feedcheck") == "Yes" and res.get("bm_id")

                if self.mode != "phase2":
                    if not is_success:
                        reason = res.get('reason', 'Failed')
                        if reason not in ("Low Content", "Low content", "Parked", "LLM failed", "Missing Phase 1 inputs"):
                            if "LLM Refused" not in reason:
                                reason = "Unable To Scrap"
                        pipeline_logger.error(f"PIPELINE FAILED: {domain} | {reason}")
                        await r_q.put({'range': f"H{idx}", 'values': [[reason]]})

                if self.mode != "phase1" and (is_success or self.mode != "phase2"):
                    pipeline_logger.info(f"PIPELINE: Updating Tracxn for {domain}")
                    dp_id = row[h_map["dp_id"]]
                    funnel_id = row[h_map["funnel_id"]]
                    
                    async def update_dp():
                        sd = res.get("sd") if is_success else None
                        ld = res.get("ld") if is_success else None
                        if sd and ld and not sd.startswith("REFUSAL:") and sd != "NO_DATA" and sd != "PARKED_LLM":
                            hashtags = [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else []
                            tags = hashtags + ["bu_llm_typec_autopublish", "bu_llm_sd_ld"]
                            payload = {"id": dp_id, "description": {"value": ld}, "shortDescription": {"value": sd}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}, "status": {"value": "PUBLISHED"}}
                            
                            company_name = res.get("company_name", "").strip()
                            if company_name:
                                payload["companyName"] = {"value": company_name}
                                
                            try:
                                eh_status, eh_res = await call_tracxn_api(
                                    session, 
                                    f"https://platform.tracxn.com/data/edithistory/edits/DOMAIN_PROFILE/{dp_id}", 
                                    tracxn_limiter, method="get", headers=HEADERS
                                )
                                if eh_status == 200 and isinstance(eh_res, list):
                                    for item in eh_res:
                                        a_name = item.get("attributeName")
                                        if a_name in ("foundedYear", "companyLocation") and item.get("createdBy") == "publish.edits@tracxn.com":
                                            payload[a_name] = {"value": None}
                                            pipeline_logger.info(f"BOT CLEANUP: Clearing {a_name} for {domain}")
                            except Exception as eh_err:
                                pipeline_logger.error(f"Failed to fetch edit history for {domain}: {eh_err}")
                                
                            return await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data=payload, headers=HEADERS)
                        return 200, None
                        
                    async def update_bm():
                        feed_id = res.get("feed_id") or (row[h_map["feed_id"]] if len(row) > h_map["feed_id"] else "")
                        if feed_id:
                            return await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/3.0/w/theme-company-association", tracxn_limiter, method="put", json_data={"object": {"themeId": feed_id, "status": "PUBLISHED", "companyId": dp_id}, "opType": "Update"}, headers=HEADERS)
                        return 200, None
                        
                    async def update_funnel():
                        feed_id = res.get("feed_id") or (row[h_map["feed_id"]] if len(row) > h_map["feed_id"] else "")
                        f_id_to_move = "5dc586332799a51cc0ff2e36" if feed_id else "591d37b884ae06633a652496"
                        As, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/force-assign", tracxn_limiter, method="put", json_data={"funnelId": funnel_id, "domainProfileId": dp_id, "sourceDetails": {"source": "Write API"}, "comment": "This is done by Write API"}, headers=HEADERS)
                        if As in (200, 201):
                            ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": funnel_id, "domainProfileId": dp_id, "movedTo": [f_id_to_move], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                            if ms == 400 and f_id_to_move != "64197f01a6dcff6572453ead":
                                ms2, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": funnel_id, "domainProfileId": dp_id, "movedTo": ["64197f01a6dcff6572453ead"], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                                return ms2
                            return ms
                        return "Assign Failed"
                        
                    (s1, _), (s_f, _), ms = await asyncio.gather(update_dp(), update_bm(), update_funnel())
                    
                    sd = res.get("sd") if is_success else None
                    ld = res.get("ld") if is_success else None
                    if sd and ld and sd != "NO_DATA" and sd != "PARKED_LLM":
                        sdld = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                    else:
                        sdld = "NotUpdated"
                        
                    feed_id = res.get("feed_id") or (row[h_map["feed_id"]] if len(row) > h_map["feed_id"] else "")
                    if feed_id:
                        f_stat = "Done" if s_f in (200, 201) else ("Duplicate/Already Moved" if s_f == 422 else ("Funnel State Conflicts" if s_f == 400 else str(s_f)))
                        fun = "Done" if ms in (200, 201) else ("Assign Failed" if ms == "Assign Failed" else ("Funnel State Conflicts" if ms == 400 else "Err"))
                    else:
                        f_stat = "N/A"
                        fun = "Sent discovery" if ms in (200, 201) else ("Assign Failed" if ms == "Assign Failed" else ("Funnel State Conflicts" if ms == 400 else "Err"))
                    
                    await r_q.put({'range': f"K{idx}:N{idx}", 'values': [[feed_id, sdld, f_stat, fun]]})
                    
                await r_q.put({'type': 'progress', 'is_success': is_success})
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
        processed_indices, success, fail = set(), 0, 0
        batch_in, batch_out, batch_think, batch_rows, batch_calls = 0, 0, 0, 0, 0
        updates = []
        last_flush = time.time()
        
        # Open CSV backup early so it's always available
        import csv
        csv_path = os.path.join(LOGS_DIR, 'results_backup.csv')
        file_exists = os.path.exists(csv_path)
        if not file_exists:
            with open(csv_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["Range", "Value1", "Value2", "Value3", "Value4", "Value5", "Value6", "Value7", "Value8"])

        async def _flush_to_sheets():
            nonlocal batch_in, batch_out, batch_think, batch_rows, batch_calls, updates, last_flush
            if not updates:
                return
            
            system_logger.info(f"SHEET WRITER FLUSHING. Updates: {len(updates)}, Time since flush: {time.time() - last_flush:.1f}s")
            try:
                def get_row_num(u):
                    m = re.search(r'\d+', u.get('range', ''))
                    return int(m.group()) if m else 0
                updates.sort(key=get_row_num)
                
                for attempt in range(3):
                    try:
                        await asyncio.wait_for(ws.batch_update(updates, value_input_option='USER_ENTERED'), timeout=60)
                        break
                    except asyncio.TimeoutError:
                        pipeline_logger.warning(f"Google Sheets timeout on attempt {attempt+1}/3. Retrying...")
                        await asyncio.sleep(2)
                    except Exception as e:
                        pipeline_logger.warning(f"Google Sheets error on attempt {attempt+1}/3: {e}. Retrying...")
                        await asyncio.sleep(2)
                else:
                    pipeline_logger.error("SHEET WRITER ERR: Failed to update Google Sheets after 3 attempts.")
                
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
                            await asyncio.wait_for(t_ws.batch_update([
                                {'range': 'B2', 'values': [[curr_in + b_in]]},
                                {'range': 'B3', 'values': [[curr_out + b_out]]},
                                {'range': 'B4', 'values': [[curr_think + b_think]]},
                                {'range': 'B5', 'values': [[curr_rows + b_rows]]},
                                {'range': 'B6', 'values': [[curr_calls + b_calls]]}
                            ], value_input_option='USER_ENTERED'), timeout=30)
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

        try:
            while True:
                try:
                    # Wait up to 1.0s for an item
                    item = await asyncio.wait_for(r_q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                else:
                    # Process items from queue
                    items_to_process = [item]
                    while not r_q.empty() and len(items_to_process) < 50:
                        items_to_process.append(r_q.get_nowait())
                    
                    for i in items_to_process:
                        if isinstance(i, dict):
                            if i.get('type') == 'progress':
                                if i.get('is_success'): success += 1
                                else: fail += 1
                                r_q.task_done()
                            elif i.get('type') == 'tokens':
                                batch_in += i.get('in', 0)
                                batch_out += i.get('out', 0)
                                batch_think += i.get('think', 0)
                                batch_rows += i.get('rows', 0)
                                batch_calls += i.get('calls', 0)
                                r_q.task_done()
                            else:
                                updates.append(i)
                                # Immediate write-ahead to CSV
                                try:
                                    with open(csv_path, 'a', newline='', encoding='utf-8') as f:
                                        writer = csv.writer(f)
                                        vals = i.get('values', [[]])[0]
                                        writer.writerow([i.get('range', '')] + [str(v)[:1000] for v in vals])
                                except Exception as e:
                                    pipeline_logger.error(f"CSV BACKUP ERR: {e}")
                                system_logger.info(f"SHEET WRITER appended item for range: {i.get('range', 'Unknown')}. Total updates: {len(updates)}")
                
                time_since_flush = time.time() - last_flush
                if updates and (len(updates) >= 10 or time_since_flush > 10 or (success + fail) == total):
                    await _flush_to_sheets()
                else:
                    current_completed = success + fail
                    self.report_progress(current_completed, total, success, fail)
        except asyncio.CancelledError:
            pipeline_logger.info("Sheet writer cancelled, flushing remaining updates...")
            if updates:
                await _flush_to_sheets()
            raise

    def report_progress(self, curr, total, success, fail):
        try:
            with open(".progress.json", "w") as f: json.dump({"current": curr, "total": total, "success": success, "fail": fail, "status": f"Rate limit hit - waiting {int(tracxn_limiter.current_wait_sec//60)} mins" if tracxn_limiter.current_wait_sec > 60 else ("Rate limit hit - waiting <1 min" if tracxn_limiter.current_wait_sec > 0 else "running")}, f)
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
