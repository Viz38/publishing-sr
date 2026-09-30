import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
import signal
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Set, Tuple, Optional, Union, Any
from urllib.parse import urlparse
from dotenv import load_dotenv

from sr_common.config import settings
from sr_common.utils import (
    call_gemini_api, call_tracxn_api, get_dynamic_max_workers, SystemHealthMonitor, 
    GeminiCacheManager, clean_html, is_parked_domain,
    update_manual_curation_date, is_tc_scraper_mode, col_to_index,
    reconcile_sheet_gaps, TrackingCacheManager, get_optimized_tcp_connector
)
from sr_common.clients import RateLimiter, MultiTierRateLimiter, GoogleSheetsClient
from sr_common.fetcher import StealthFetcher, BrowserManager
from sr_common.supabase_client import fetch_scraped_content
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
    handler = RotatingFileHandler(log_file, maxBytes=50*1024*1024, backupCount=5, encoding='utf-8')
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
        RotatingFileHandler(os.path.join(LOGS_DIR, 'api.logs'), maxBytes=50*1024*1024, backupCount=5, encoding='utf-8')
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
tracxn_limiter = MultiTierRateLimiter(os.path.join(LOGS_DIR, 'tracxn_rate_limit.db'), {'second': 95})

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


async def process_domain_stage1(browser, session, row, prompts, f_ids, h_map, cache_manager) -> Dict:
    domain = row[h_map["domain"]]
    pipeline_logger.info(f"PROCESS START: {domain}")
    
    raw_data = row[17] if len(row) > 17 else ""
    
    scraper_used = "Manual"
    if raw_data.strip():
        pipeline_logger.info(f"PROCESS: Found raw data for {domain}, skipping scrape.")
        final_url = f"https://{domain}"
        body = raw_data.strip()
    else:
        # Check Scraper Type from row (Col S = index 18)
        scraper_idx = h_map.get("scraper_idx", 18)
        raw_scraper_type = row[scraper_idx].strip() if len(row) > scraper_idx else ""
        use_tc = is_tc_scraper_mode(raw_scraper_type)
        
        body = None
        if use_tc:
            # TC Mode: Check Supabase (Tech Crawler)
            supabase_content = await fetch_scraped_content(domain)
            if supabase_content:
                body = supabase_content
                scraper_used = "Tech Crawler"
                pipeline_logger.info(f"DATASOURCE: {domain} → Tech Crawler (Supabase, {len(body)} chars)")
            else:
                pipeline_logger.info(f"DATASOURCE: {domain} → BU (built-in scraper fallback from TC miss)")
        
        # BU Mode (or TC miss fallback): run built-in StealthFetcher
        if not body:
            scraper_used = "BU"
            if not use_tc:
                pipeline_logger.info(f"DATASOURCE: {domain} → BU (built-in scraper)")
            html, final_url, reason = await fetcher.fetch(browser, f"https://{domain}")

            if html is None:
                pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: {reason}")
                return {"type": "error", "reason": reason, "scraper_used": scraper_used}
            if len(html) < 300:
                pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: Low Content ({len(html)} chars)")
                return {"type": "error", "reason": "Low Content", "scraper_used": scraper_used}
                
            body = await clean_html(html)
            pipeline_logger.info(f"PROCESS: Scraped {domain} | Length: {len(body)}")
            
            # Check for parked
            parked, kw = is_parked_domain(html, body)
            if parked:
                pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked ({kw})")
                return {"type": "error", "reason": "Parked", "scraper_used": scraper_used}
    
    if len(body) < 100: 
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Low content")
        return {"type": "error", "reason": "Low content", "scraper_used": scraper_used}
        
    llm_calls = 0
    llm_rows = 1
    
    parts_p1 = prompts[0].split("XX")
    if len(parts_p1) == 2:
        sys_p1 = parts_p1[0].strip() + "\n\n[DATA PROVIDED BY USER BELOW]\n\n" + parts_p1[1].strip()
        user_p1 = "Raw Content:\n" + body[:20000]
        cache_id = await cache_manager.get_or_create(session, "prompt_0", sys_p1, ttl="86400s")
        res_obj = await call_gemini_api(session, user_p1, gemini_limiter, api_key=CONFIG["GEMINI_API_KEY"], system_instruction=sys_p1, cached_content_name=cache_id, cache_manager=cache_manager, cache_key="prompt_0")
    else:
        p1 = prompts[0].replace("XX", body[:20000])
        res_obj = await call_gemini_api(session, p1, gemini_limiter, api_key=CONFIG["GEMINI_API_KEY"])
    
    llm_calls += 1
    res, in_p, out_p = res_obj.text, res_obj.prompt_tokens, res_obj.candidate_tokens
    think_tokens = res_obj.thinking_tokens
    think_text = res_obj.thinking_text
    
    tokens = {"in": in_p, "out": out_p, "think": think_tokens}
    
    from sr_common.utils import extract_descriptions
    sd, ld = extract_descriptions(res)
    if sd == "NO_DATA":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Insufficient content (AI reported NO_DATA)")
        return {"type": "error", "reason": "Low content", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows, "scraper_used": scraper_used}
    if sd == "PARKED_LLM":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked (AI reported PARKED_LLM)")
        return {"type": "error", "reason": "Parked", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows, "scraper_used": scraper_used}
    if not sd or not ld:
        pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: LLM failed to generate descriptions")
        return {"type": "error", "reason": "LLM failed - missing descriptions", "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows, "scraper_used": scraper_used}

    pipeline_logger.info(f"PROCESS SUCCESS: {domain}")
    f_id = f_ids.get(row[h_map["funnel_name"]].split(" : ")[1] if " : " in row[h_map["funnel_name"]] else row[h_map["funnel_name"]], "")
    return {
        "type": "success", "dp_id": row[h_map["dp_id"]], "funnel_id": row[h_map["funnel_id"]], "feed_id": f_id,
        "sd": sd, "ld": ld, "tokens": tokens, "llm_calls": llm_calls, "llm_rows": llm_rows,
        "company_name": row[h_map["company_name"]] if len(row) > h_map["company_name"] else "",
        "funnel_name": row[h_map["funnel_name"]] if len(row) > h_map["funnel_name"] else "",
        "body_len": len(body), "scraper_used": scraper_used,
        "raw_data": body if scraper_used == "Tech Crawler" else ""
    }

class TypeCPipeline:
    def __init__(self, start_row: int, mode: str):
        self.start_row = start_row
        self.mode = mode
        self.config = CONFIG.copy()
        self.apply_formatting = True
        self.stop_requested = False

    async def _stop_monitor(self, w_q=None):
        """Monitors for graceful stop request via SIGTERM or .stop_requested file.
        When stop is requested, sets stop_requested=True so workers complete current
        work and safely exit without discarding queued items."""
        while not (self.stop_requested or os.path.exists(".stop_requested")):
            await asyncio.sleep(0.5)
        self.stop_requested = True
        pipeline_logger.warning("GRACEFUL STOP TRIGGERED: Halting domain workers (preserving work_queue)...")

    async def run(self):
        pipeline_logger.info(f"PIPELINE START: Row {self.start_row} | Mode: {self.mode}")
        self.report_progress(0, 0, 0, 0)
        # Start system monitoring
        asyncio.create_task(log_system_metrics())

        loop = asyncio.get_running_loop()
        def _on_sigterm():
            pipeline_logger.warning("SIGTERM RECEIVED: Initiating graceful stop...")
            self.stop_requested = True
        try:
            loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
        except (NotImplementedError, RuntimeError):
            pass
        
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
            "sd": 8, "ld": 9, "feed_id": 10, "funnel_name": 3, "r1": "H",
            "scraper_col": "S", "scraper_idx": 18
        }
        
        all_rows = await ws.get_values()
        data_rows = []
        for i, r in enumerate(all_rows[self.start_row-1:], start=self.start_row):
            if len(r) > 1 and r[1].strip():
                data_rows.append((i, r))
        
        if data_rows:
            max_idx = max(idx for idx, _ in data_rows)
            if max_idx > ws.row_count:
                needed = max_idx - ws.row_count + 100
                pipeline_logger.info(f"Expanding worksheet grid: adding {needed} rows...")
                await ws.add_rows(needed)
        
        p_sheet = await gc.open_by_key(self.config["PROMPTS_SHEET_ID"])
        prompts = [r[1] for r in (await (await p_sheet.worksheet("Prompts")).get_values())[1:10]]
        fo_sheet = await gc.open_by_key(self.config["FEED_OWNER_SHEET_ID"])
        f_ids = {r[0]: r[1] for r in (await (await fo_sheet.worksheet("Feed Owner Details")).get_values())}

        cache_manager = TrackingCacheManager(settings.TYPEC_GEMINI_API_KEY, stats_filepath=os.path.join(LOGS_DIR, "cache_stats.json"))
        async with aiohttp.ClientSession(connector=get_optimized_tcp_connector()) as session:
            work_queue, result_queue, tracxn_queue = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
            for idx, row in data_rows:
                await work_queue.put((idx, row))

            stop_monitor_task = asyncio.create_task(self._stop_monitor(work_queue))
            writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows), gc, "TypeC", tracxn_queue))
            heartbeat_task = asyncio.create_task(self.heartbeat_reporter(tracxn_queue))
            tracxn_workers = [
                asyncio.create_task(self.tracxn_worker(tracxn_queue, result_queue, session))
                for _ in range(5)
            ]

            if self.mode == "phase2":
                tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, tracxn_queue, None, session, prompts, f_ids, h_map, cache_manager)) for _ in range(CONFIG["MAX_WORKERS"])]
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                browser_mgr = BrowserManager(max_navigations=150)
                try:
                    pipeline_logger.info(f"Starting continuous streaming pipeline for {len(data_rows)} domains with {CONFIG['MAX_WORKERS']} workers")
                    tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, tracxn_queue, browser_mgr, session, prompts, f_ids, h_map, cache_manager)) for _ in range(CONFIG["MAX_WORKERS"])]
                    await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    await browser_mgr.close()

            if self.mode != "phase1":
                pipeline_logger.info(f"Draining Tracxn queue ({tracxn_queue.qsize()} pending)...")
                await tracxn_queue.join()
            for t in tracxn_workers:
                t.cancel()

            await result_queue.join()
            writer_task.cancel()
            heartbeat_task.cancel()
            stop_monitor_task.cancel()

            # End-of-run Gap Reconciliation & Recovery Pass
            if not self.stop_requested and not os.path.exists(".stop_requested"):
                pipeline_logger.info("AUDIT: Verifying sheet completion and checking for gaps...")
                try:
                    reconciled, still_missing = await reconcile_sheet_gaps(
                        ws=ws,
                        start_row=self.start_row,
                        data_rows=data_rows,
                        csv_records=os.path.join(LOGS_DIR, 'results_backup.csv'),
                        h_map=h_map,
                        stat_col=h_map.get("r1", "H")
                    )
                    if reconciled:
                        pipeline_logger.info(f"AUDIT: Successfully reconciled {len(reconciled)} rows from backup CSV.")
                    if still_missing:
                        pipeline_logger.warning(f"AUDIT: Found {len(still_missing)} unwritten rows. Initiating recovery pass...")
                        recovery_updates = []
                        stat_col = h_map.get("r1", "H")
                        for m_idx in still_missing:
                            recovery_updates.append({
                                'range': f"{stat_col}{m_idx}",
                                'values': [["Failed: Unprocessed in run"]]
                            })
                        if recovery_updates:
                            await ws.batch_update(recovery_updates)
                            pipeline_logger.info(f"AUDIT: Marked {len(recovery_updates)} missing rows to ensure zero blank gaps.")
                    else:
                        pipeline_logger.info("AUDIT: 100% rows verified and complete in Google Sheet.")
                except Exception as audit_err:
                    pipeline_logger.error(f"AUDIT ERROR: {audit_err}")

            if os.path.exists(".stop_requested"):
                try:
                    os.remove(".stop_requested")
                except Exception:
                    pass

    async def heartbeat_reporter(self, t_q):
        """Background task updating rate limit sleep countdown and backlog in .progress.json every second."""
        while True:
            try:
                pause_info = tracxn_limiter.get_pause_status()
                current_data = {"current": 0, "total": 0, "success": 0, "fail": 0}
                if os.path.exists(".progress.json"):
                    try:
                        with open(".progress.json", "r") as f:
                            current_data = json.load(f)
                    except Exception:
                        pass
                
                current_data["backlog"] = t_q.qsize() if t_q else 0
                current_data["rate_limit_paused"] = pause_info["paused"]
                current_data["sleep_remaining_sec"] = pause_info["remaining"]
                current_data["rate_limit_message"] = pause_info["message"]
                current_data["is_stopping"] = self.stop_requested or os.path.exists(".stop_requested")
                
                tmp_file = f".progress.json.tmp.{os.getpid()}"
                with open(tmp_file, "w") as f:
                    json.dump(current_data, f)
                os.replace(tmp_file, ".progress.json")
            except Exception:
                pass
            await asyncio.sleep(1.0)

    async def domain_worker(self, w_q, r_q, t_q, browser, session, prompts, f_ids, h_map, cache_manager):
        monitor = SystemHealthMonitor()
        import random
        await asyncio.sleep(random.uniform(1.0, 5.0))
        while True:
            if self.stop_requested or os.path.exists(".stop_requested"):
                pipeline_logger.info("STOP INITIATED: Domain worker halting Stage 1.")
                break
            try:
                idx, row = await asyncio.wait_for(w_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if w_q.empty():
                    break
                continue

            domain = row[h_map["domain"]] if row and "domain" in h_map and len(row) > h_map["domain"] else "Unknown"
            try:
                await monitor.wait_for_resources(logger=pipeline_logger, timeout=300, fast_fail_ram=True)
                domain = row[h_map["domain"]]
                date_str = datetime.now().strftime("%d-%b-%Y")
                await r_q.put({'range': f"A{idx}", 'values': [[date_str]]})
                if self.mode == "phase2":
                    sd, ld, dp_id, funnel_id = row[h_map["sd"]], row[h_map["ld"]], row[h_map["dp_id"]], row[h_map["funnel_id"]]
                    scrap_stat = row[7] if len(row) > 7 else ""
                    if not (sd and ld and dp_id and funnel_id and scrap_stat.startswith("Yes")):
                        existing_reason = scrap_stat.strip()
                        if existing_reason and not existing_reason.startswith("Yes"):
                            res = {"type": "failed", "reason": existing_reason}
                        else:
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
                else:
                    try:
                        res = await asyncio.wait_for(
                            process_domain_stage1(browser, session, row, prompts, f_ids, h_map, cache_manager),
                            timeout=75.0
                        )
                    except asyncio.TimeoutError:
                        pipeline_logger.error(f"DOMAIN TIMEOUT: {domain} exceeded 75s Stage 1 budget")
                        res = {"type": "error", "reason": "Timeout", "scraper_used": "BU"}
                
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
                        
                        scraper_used = res.get("scraper_used", "BU")
                        await r_q.put({'range': f"S{idx}", 'values': [[scraper_used]]})
                        if scraper_used == "Tech Crawler" and res.get("raw_data"):
                            await r_q.put({'range': f"R{idx}", 'values': [[res.get("raw_data")[:40000]]]})

                is_success = res["type"] == "success"
                is_full_success = is_success and res.get("feedcheck") == "Yes" and res.get("bm_id")

                if self.mode != "phase2":
                    if not is_success:
                        reason = res.get('reason', 'Failed')
                        if not reason.startswith("LLM failed") and reason not in ("Low Content", "Low content", "Parked") and not reason.startswith("Missing"):
                            reason = "Unable To Scrap"
                        pipeline_logger.error(f"PIPELINE FAILED: {domain} | {reason}")
                        await r_q.put({'range': f"H{idx}", 'values': [[reason[:500]]]})
                        
                        scraper_used = res.get("scraper_used", "BU")
                        await r_q.put({'range': f"S{idx}", 'values': [[scraper_used]]})

                if self.mode != "phase1":
                    # Memory optimization: release large raw scraped HTML/text before passing to Tracxn queue
                    res.pop("raw_data", None)
                    await t_q.put({
                        'idx': idx,
                        'row': row,
                        'res': res,
                        'is_success': is_success,
                        'is_full_success': is_full_success,
                        'h_map': h_map,
                        'f_ids': f_ids
                    })
                else:
                    await r_q.put({'type': 'progress', 'is_success': is_success})
            except Exception as e:
                if "Resource saturation" in str(e):
                    import gc, random
                    gc.collect()
                    backoff = random.uniform(6.0, 12.0)
                    pipeline_logger.warning(f"Re-queuing row {idx} due to Resource Saturation. Backing off {backoff:.1f}s...")
                    await w_q.put((idx, row))
                    await asyncio.sleep(backoff)
                else:
                    pipeline_logger.error(f"FATAL WORKER ERROR for {row[h_map['domain']] if row else 'Unknown'}: {e}")
                    if self.mode != "phase2":
                        stat_col = h_map.get("r1", "H")
                        await r_q.put({'range': f"{stat_col}{idx}", 'values': [[f"Fatal Error: {str(e)[:50]}"]]})
                    await r_q.put({'type': 'progress', 'is_success': False})
            finally:
                w_q.task_done()

    async def tracxn_worker(self, t_q, r_q, session):
        """Asynchronous worker dedicated to Tracxn API updates.
        Pulls from t_q, processes updates, and handles rate limit cooldowns
        without blocking web scraping or LLM operations."""
        while True:
            item = await t_q.get()
            try:
                idx = item['idx']
                row = item['row']
                res = item['res']
                is_success = item['is_success']
                is_full_success = item['is_full_success']
                h_map = item['h_map']
                f_ids = item['f_ids']
                domain = row[h_map["domain"]]

                pipeline_logger.info(f"PIPELINE: Updating Tracxn for {domain}")
                dp_id = row[h_map["dp_id"]]
                funnel_id = row[h_map["funnel_id"]]
                
                ht_val_init = res.get("hashtags")
                if ht_val_init is None:
                    initial_hashtags = [t.strip() for t in row[h_map["tags"]].split(",")] if row and "tags" in h_map and len(row) > h_map["tags"] and row[h_map["tags"]] else []
                elif isinstance(ht_val_init, str):
                    initial_hashtags = [t.strip() for t in ht_val_init.split(",") if t.strip()]
                else:
                    initial_hashtags = list(ht_val_init)
                
                has_incorrect_pred_tag = any(h.lower() == "bu_llm_sr_incorrectprediction" for h in initial_hashtags)

                async def update_dp():
                    feed_id = res.get("feed_id") or (row[h_map["feed_id"]] if len(row) > h_map["feed_id"] else "")
                    sd = res.get("sd") if is_success else None
                    ld = res.get("ld") if is_success else None
                    if sd and ld and sd != "NO_DATA" and sd != "PARKED_LLM":
                        hashtags = list(initial_hashtags)
                        tags = hashtags + ["bu_llm_typec_autopublish", "bu_llm_sd_ld"]
                        
                        if feed_id:
                            tags = [h for h in tags if h.lower() != "bu_llm_sr_incorrectprediction"]
                        else:
                            if "bu_llm_sr_incorrectprediction" not in tags:
                                tags.append("bu_llm_sr_incorrectprediction")
                                
                        res["hashtags"] = tags
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
                                for item_eh in eh_res:
                                    a_name = item_eh.get("attributeName")
                                    if a_name in ("foundedYear", "companyLocation") and item_eh.get("createdBy") == "publish.edits@tracxn.com":
                                        payload[a_name] = {"value": None}
                                        pipeline_logger.info(f"BOT CLEANUP: Clearing {a_name} for {domain}")
                        except Exception as eh_err:
                            pipeline_logger.error(f"Failed to fetch edit history for {domain}: {eh_err}")
                            
                        return await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data=payload, headers=HEADERS)
                    else:
                        hashtags = list(initial_hashtags)
                        fail_reason = res.get('reason', 'Failed') if not is_success else ''

                        if fail_reason.startswith("Missing"):
                            if "bu_llm_sr_incorrectprediction" not in hashtags:
                                hashtags.append("bu_llm_sr_incorrectprediction")
                        else:
                            if "bu_llm_sr_lowwebscrap" not in hashtags:
                                hashtags.append("bu_llm_sr_lowwebscrap")

                        res["hashtags"] = hashtags

                        payload = {
                            "id": dp_id, 
                            "keywords": {"value": {"HASHTAGS": hashtags}}
                        }
                        return await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data=payload, headers=HEADERS)
                    
                async def update_bm():
                    feed_id = res.get("feed_id") or (row[h_map["feed_id"]] if len(row) > h_map["feed_id"] else "")
                    if feed_id:
                        return await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/3.0/w/theme-company-association", tracxn_limiter, method="put", json_data={"object": {"themeId": feed_id, "status": "PUBLISHED", "companyId": dp_id}, "opType": "Update"}, headers=HEADERS)
                    return 200, None
                    
                async def update_funnel(feed_status):
                    feed_id = res.get("feed_id") or (row[h_map["feed_id"]] if len(row) > h_map["feed_id"] else "")
                    is_full_success_typec = bool(feed_id and is_success)
                    if is_full_success_typec:
                        f_id_to_move = "5dc586332799a51cc0ff2e36"
                    else:
                        if not is_success:
                            fail_reason = res.get('reason', 'Failed')
                        else:
                            fail_reason = "Missing BM match"
                            
                        sd_check = res.get("sd") if is_success else None
                        is_low_webscrap = (
                            not is_success or sd_check in (None, "", "NO_DATA", "PARKED_LLM")
                        ) and not fail_reason.startswith("Missing")
                        
                        if is_low_webscrap:
                            # Low webscraping/parked issues snooze
                            As, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/force-assign", tracxn_limiter, method="put", json_data={"funnelId": funnel_id, "domainProfileId": dp_id, "sourceDetails": {"source": "Write API"}, "comment": "This is done by Write API"}, headers=HEADERS)
                            if As in (200, 201):
                                Snooz_noweb, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/snooze", tracxn_limiter, method="put", json_data={"funnelId": funnel_id, "domainProfileId": dp_id, "snoozeMethod": {"type": "period", "value": "180"}, "sourceDetails": {"source": "Write API"}, "comment": "This is done by Write API"}, headers=HEADERS)
                                if Snooz_noweb in (200,201):
                                    return "Snoozed"
                                else:
                                    return "Snooze Failed"
                            else:
                                return "Assign Failed"
                        else:
                            # Irrelevant / prediction failure cases: only move if hashtag was already present
                            if has_incorrect_pred_tag:
                                As, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/force-assign", tracxn_limiter, method="put", json_data={"funnelId": funnel_id, "domainProfileId": dp_id, "sourceDetails": {"source": "Write API"}, "comment": "This is done by Write API"}, headers=HEADERS)
                                if As in (200, 201):
                                    Snooz_noweb, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/snooze", tracxn_limiter, method="put", json_data={"funnelId": funnel_id, "domainProfileId": dp_id, "snoozeMethod": {"type": "period", "value": "180"}, "sourceDetails": {"source": "Write API"}, "comment": "This is done by Write API"}, headers=HEADERS)
                                    if Snooz_noweb in (200,201):
                                        return "Snoozed"
                                    else:
                                        return "Snooze Failed"
                                else:
                                    return "Assign Failed"
                            else:
                                f_id_to_move = "64197f01a6dcff6572453ead"

                    As, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/force-assign", tracxn_limiter, method="put", json_data={"funnelId": funnel_id, "domainProfileId": dp_id, "sourceDetails": {"source": "Write API"}, "comment": "This is done by Write API"}, headers=HEADERS)
                    if As in (200, 201):
                        if f_id_to_move == "5dc586332799a51cc0ff2e36" and feed_status != 422:
                            await update_manual_curation_date(session, dp_id, tracxn_limiter, HEADERS)
                        ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": funnel_id, "domainProfileId": dp_id, "movedTo": [f_id_to_move], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                        if ms == 400 and f_id_to_move != "64197f01a6dcff6572453ead":
                            ms2, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": funnel_id, "domainProfileId": dp_id, "movedTo": ["64197f01a6dcff6572453ead"], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                            return ms2
                        return ms
                    return "Assign Failed"
                    
                (s1, _), (s_f, _) = await asyncio.gather(update_dp(), update_bm())
                ms = await update_funnel(s_f)
                
                feed_id_check = res.get("feed_id") or (row[h_map["feed_id"]] if len(row) > h_map["feed_id"] else "")
                is_full_success_check = bool(feed_id_check and is_success)
                if not is_success:
                    fail_reason = res.get('reason', 'Failed')
                elif not is_full_success_check:
                    fail_reason = "Missing BM match"
                else:
                    fail_reason = ""
                    
                pipeline_logger.info(f"TEST LOG [{domain}]: is_success={is_success} | is_full_success={is_full_success_check} | fail_reason='{fail_reason}' | has_incorrect_pred_tag={has_incorrect_pred_tag} | funnel_status={ms}")
                
                if is_success:
                    not_updated_text = "NotUpdated"
                elif fail_reason in ("Low Content", "Low content"):
                    not_updated_text = "Low Content"
                elif fail_reason == "Parked":
                    not_updated_text = "Parked"
                elif fail_reason.startswith("Missing"):
                    not_updated_text = "Irrelevant"
                elif fail_reason.startswith("LLM failed") or fail_reason == "Unable To Scrap":
                    not_updated_text = "NotUpdated"
                else:
                    not_updated_text = "Irrelevant"
                
                sd = res.get("sd") if is_success else None
                ld = res.get("ld") if is_success else None
                if sd and ld and sd != "NO_DATA" and sd != "PARKED_LLM":
                    sdld = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                else:
                    sdld = not_updated_text
                    
                feed_id = res.get("feed_id") or (row[h_map["feed_id"]] if len(row) > h_map["feed_id"] else "")
                if feed_id:
                    f_stat = "Done" if s_f in (200, 201) else ("Duplicate/Already Moved" if s_f == 422 else ("Funnel State Conflicts" if s_f == 400 else str(s_f)))
                    fun = "Done" if ms in (200, 201) else ("Assign Failed" if ms == "Assign Failed" else ("Funnel State Conflicts" if ms == 400 else "Err"))
                else:
                    f_stat = "N/A"
                    if ms in (200, 201):
                        fun = "Sent discovery"
                    elif ms == "Snoozed":
                        fun = "Snoozed"
                    elif ms == "Snooze Failed":
                        fun = "Snooze Failed"
                    else:
                        fun = "Assign Failed" if ms == "Assign Failed" else ("Funnel State Conflicts" if ms == 400 else "Err")
                
                await r_q.put({'range': f"K{idx}:N{idx}", 'values': [[feed_id, sdld, f_stat, fun]]})
                await r_q.put({'type': 'progress', 'is_success': is_success})
            except Exception as e:
                d_idx = h_map.get('domain', 0) if isinstance(h_map, dict) else 0
                row_data = item.get('row')
                err_domain = row_data[d_idx] if isinstance(row_data, list) and len(row_data) > d_idx else 'unknown'
                pipeline_logger.error(f"TRACXN WORKER ERROR for {err_domain}: {e}")
                if 'idx' in item:
                    await r_q.put({'range': f"K{item['idx']}:N{item['idx']}", 'values': [["", "Err Tracxn", "Err Tracxn", "Err Tracxn"]]})
                await r_q.put({'type': 'progress', 'is_success': False})
            finally:
                t_q.task_done()

    async def sheet_writer(self, r_q, ws, total, gc, pipeline_name, t_q=None):
        processed_indices, success, fail = set(), 0, 0
        batch_in, batch_out, batch_think, batch_rows, batch_calls = 0, 0, 0, 0, 0
        updates = []
        last_flush = time.time()
        _tracking_ws = None

        async def _get_tracking_ws():
            nonlocal _tracking_ws
            if _tracking_ws is None:
                t_sheet = await gc.open_by_key(CONFIG["TRACKING_SHEET_ID"])
                _tracking_ws = await t_sheet.worksheet(pipeline_name)
            return _tracking_ws
        
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
                    return int(m.group(0)) if m else 0
                
                def clean_range(r):
                    return r.split("!")[-1]
                
                def sanitize_val(v):
                    if isinstance(v, str) and len(v) > 45000:
                        return v[:45000]
                    return v

                # Deduplicate updates
                latest_updates = {}
                for u in updates:
                    latest_updates[u['range']] = u
                updates_to_send = list(latest_updates.values())
                updates_to_send.sort(key=get_row_num)
                
                for attempt in range(5):
                    try:
                        clean_payload = [
                            {
                                'range': clean_range(u['range']),
                                'values': [[sanitize_val(val) for val in row] for row in u.get('values', [[]])]
                            }
                            for u in updates_to_send
                        ]
                        await asyncio.wait_for(ws.batch_update(clean_payload, value_input_option='USER_ENTERED'), timeout=120)
                        system_logger.info(f"SHEET WRITER FLUSH SUCCESSFUL for {len(updates_to_send)} ranges.")
                        break
                    except Exception as e:
                        if attempt == 3:
                            # Fallback: Attempt chunked updates (batches of 5) to isolate any single rogue update
                            pipeline_logger.warning(f"Batch flush failed on attempt {attempt+1}: {e}. Falling back to chunked updates...")
                            try:
                                chunk_size = 5
                                for ci in range(0, len(updates_to_send), chunk_size):
                                    sub_chunk = updates_to_send[ci:ci + chunk_size]
                                    sub_payload = [
                                        {
                                            'range': clean_range(u['range']),
                                            'values': [[sanitize_val(val) for val in row] for row in u.get('values', [[]])]
                                        }
                                        for u in sub_chunk
                                    ]
                                    try:
                                        await asyncio.wait_for(ws.batch_update(sub_payload, value_input_option='USER_ENTERED'), timeout=30)
                                    except Exception as sub_e:
                                        pipeline_logger.error(f"Chunk failed ({[u['range'] for u in sub_chunk]}): {sub_e}")
                                system_logger.info(f"SHEET WRITER CHUNKED FLUSH COMPLETED.")
                                break
                            except Exception as chunk_err:
                                pipeline_logger.error(f"Chunked fallback error: {chunk_err}")
                        if attempt == 4:
                            pipeline_logger.critical(f"FATAL: All 5 flush attempts failed. Data saved to CSV backup: {e}")
                            raise e
                        sleep_time = (attempt + 1) * 5
                        pipeline_logger.warning(f"Flush attempt {attempt+1} failed: {e}. Retrying in {sleep_time}s...")
                        await asyncio.sleep(sleep_time)
                
                for u in updates_to_send:
                    match = re.search(r'\d+', u['range'])
                    if match: processed_indices.add(int(match.group()))
                
                if (batch_in or batch_out or batch_calls):
                    b_in, b_out, b_think, b_rows, b_calls = batch_in, batch_out, batch_think, batch_rows, batch_calls
                    async def _update_tracking(b_in, b_out, b_think, b_rows, b_calls):
                        nonlocal _tracking_ws
                        try:
                            try:
                                t_ws = await _get_tracking_ws()
                                vals = await t_ws.batch_get(["B2", "B3", "B4", "B5", "B6"])
                            except Exception:
                                _tracking_ws = None
                                t_ws = await _get_tracking_ws()
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
                    
                    asyncio.create_task(_update_tracking(b_in, b_out, b_think, b_rows, b_calls))
                    batch_in, batch_out, batch_think, batch_rows, batch_calls = 0, 0, 0, 0, 0
            except Exception as e:
                pipeline_logger.error(f"SHEET WRITER ERR: {e}")
            finally:
                for _ in updates: r_q.task_done()
                updates = []
                last_flush = time.time()
                current_completed = success + fail
                self.report_progress(current_completed, total, success, fail, backlog=(t_q.qsize() if t_q else 0))
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
                    
                    csv_rows = []
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
                                vals = i.get('values', [[]])[0]
                                csv_rows.append([i.get('range', '')] + [str(v)[:1000] for v in vals])
                    
                    if csv_rows:
                        try:
                            with open(csv_path, 'a', newline='', encoding='utf-8') as f:
                                writer = csv.writer(f)
                                writer.writerows(csv_rows)
                        except Exception as e:
                            pipeline_logger.error(f"CSV BACKUP ERR: {e}")
                
                time_since_flush = time.time() - last_flush
                if updates and (len(updates) >= 10 or time_since_flush > 5 or (success + fail) == total or self.stop_requested or r_q.empty()):
                    await _flush_to_sheets()
                else:
                    current_completed = success + fail
                    self.report_progress(current_completed, total, success, fail, backlog=(t_q.qsize() if t_q else 0))
        except asyncio.CancelledError:
            pipeline_logger.info("Sheet writer cancelled, flushing remaining updates...")
            if updates:
                await _flush_to_sheets()
            raise

    def report_progress(self, curr, total, s, f, backlog=0):
        try:
            pause_info = tracxn_limiter.get_pause_status()
            data = {
                "current": curr,
                "total": total,
                "success": s,
                "fail": f,
                "backlog": backlog,
                "rate_limit_paused": pause_info["paused"],
                "sleep_remaining_sec": pause_info["remaining"],
                "rate_limit_message": pause_info["message"],
                "is_stopping": self.stop_requested or os.path.exists(".stop_requested")
            }
            tmp_file = f".progress.json.tmp.{os.getpid()}"
            with open(tmp_file, "w") as file:
                json.dump(data, file)
            os.replace(tmp_file, ".progress.json")
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
