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
from urllib.parse import urlparse
from dotenv import load_dotenv

from sr_common.config import settings
from sr_common.clients import RateLimiter, GoogleSheetsClient
from sr_common.utils import call_gemini_api, call_tracxn_api, clean_html, extract_descriptions, is_parked_domain, get_dynamic_max_workers, SystemHealthMonitor, GeminiCacheManager

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

    # --- TIER 1: SCRAPLING ---
    try:
        from scrapling import Fetcher
        scrap_logger.info(f"TIER 1: Scrapling Request Fetch for {url}")
        
        s_fetcher = Fetcher()
        s_resp = await asyncio.to_thread(s_fetcher.get, url)
        
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
                
                response = await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                
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

    # --- TIER 3: SCRAPLING STEALTH ---
    monitor = SystemHealthMonitor()
    try:
        # MID-PROCESS CHECK: Wait if CPU is redlining before launching another Playwright process
        await monitor.wait_for_resources(logger=scrap_logger)
        
        async with BROWSER_SEMAPHORE:
            from scrapling import StealthyFetcher
            scrap_logger.info(f"TIER 3: Scrapling Stealth (Playwright) for {url}")
            # NEW: use async_fetch class method to avoid sync-loop crash
            # Wrap in asyncio.wait_for to forcefully break out of scrapling's internal 3-retry 90-second hang
            s_resp = await asyncio.wait_for(
                StealthyFetcher.async_fetch(url, headless=True, timeout=CONFIG["REQUEST_TIMEOUT"] * 1000),
                timeout=35.0
            )
            
            if s_resp.status == 200:
                content = s_resp.text
                if "sgcaptcha" not in content.lower() and len(content) > 300:
                    scrap_logger.info(f"TIER 3 SUCCESS: {url} | Chars: {len(content)}")
                    return content, 200, "Success"
                return None, 200, "Captcha or Low Content"
            else:
                return None, s_resp.status, f"Status {s_resp.status}"
    except Exception as e:
        scrap_logger.error(f"TIER 3 ERR: {url} | {str(e)}")
        return None, 0, "Fetch failed"

    return None, 0, "Fetch failed"

async def process_domain_stage1(browser, session, row, prompts, f_ids, h_map, cache_manager) -> Dict:
    domain = row[h_map["domain"]]
    pipeline_logger.info(f"PROCESS START: {domain}")
    
    html, _, reason = await fetch_page(browser, f"https://{domain}")
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
        sys_p1 = parts_p1[1].strip()
        user_p1 = parts_p1[0].strip() + "\n\n" + body[:20000]
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
                # Optimize: Only fetch from start_row onwards
                all_rows = await ws.get_values(f"A{self.start_row}:Z")
                data_rows = [r for r in all_rows if len(r) > 1 and r[1].strip()]
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
        
        p_sheet = await gc.open_by_key(self.config["PROMPTS_SHEET_ID"])
        prompts = [r[1] for r in (await (await p_sheet.worksheet("Prompts")).get_all_values())[1:10]]
        fo_sheet = await gc.open_by_key(self.config["FEED_OWNER_SHEET_ID"])
        f_ids = {r[0]: r[1] for r in (await (await fo_sheet.worksheet("Feed Owner Details")).get_all_values())}

        work_queue, result_queue = asyncio.Queue(), asyncio.Queue()
        for idx, row in enumerate(data_rows, start=self.start_row):
            # No explicit skip column in Type C currently
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
                        
                        async with AsyncCamoufox(
                            headless=True,
                            humanize=True,
                            block_webrtc=True,
                            os="windows",
                            i_know_what_im_doing=True
                        ) as browser:
                            tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, browser, session, prompts, f_ids, h_map, cache_manager)) for _ in range(CONFIG["MAX_WORKERS"])]
                            writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows), gc, "TypeC"))
                            
                            # This will run until all work is done or an exception occurs
                            await work_queue.join()
                            [t.cancel() for t in tasks]
                            await result_queue.join()
                            writer_task.cancel()
                            break # Successfully finished all work
                    except Exception as e:
                        pipeline_logger.error(f"BROWSER ENGINE CRASHED: {e}. Waiting for resources to restart...")
                        # Wait and then loop back to restart AsyncCamoufox
                        await asyncio.sleep(10)
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
                await monitor.wait_for_resources(logger=pipeline_logger)
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
                            "funnel_name": row[h_map["funnel_name"]], "company_name": row[h_map["company_name"]],
                            "tags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
                            "tokens": {"in":0, "out":0, "think":0}, "body_len": int(scrap_stat.split(":")[-1]) if ":" in scrap_stat else 0
                        }
                else: res = await process_domain_stage1(browser, session, row, prompts, f_ids, h_map, cache_manager)
                
                if "tokens" in res:
                    await r_q.put({'type': 'tokens', 'in': res["tokens"]["in"], 'out': res["tokens"]["out"], 'think': res["tokens"]["think"], 'rows': res.get("llm_rows", 0), 'calls': res.get("llm_calls", 0)})
                
                if res["type"] == "success":
                    if self.mode != "phase2":
                        await r_q.put({'range': f"H{idx}:J{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld"]]]})
                        await r_q.put({'range': f"O{idx}:Q{idx}", 'values': [[res["tokens"]["in"], res["tokens"]["out"], f"Tokens: {res['tokens'].get('think', 0)}\n\n{res.get('thinking_text', '')}"]]})
                    
                    if self.mode != "phase1":
                        pipeline_logger.info(f"PIPELINE: Updating Tracxn for {domain}")
                        tags = res["tags"] + ["bu_llm_typec_autopublish"]
                        funnel_name = res.get("funnel_name") or ""
                        feed = funnel_name.split(" : ")[1] if " : " in funnel_name else funnel_name
                        feed_id = f_ids.get(feed, "")
                        
                        f_stat, sdld, fun = "N/A", "N/A", "N/A"
                        if feed_id:
                            s_f, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/3.0/w/theme-company-association", tracxn_limiter, method="put", json_data={"object": {"themeId": feed_id, "status": "PUBLISHED", "companyId": res["dp_id"]}, "opType": "Update"}, headers=HEADERS)
                            f_stat = "Done" if s_f in (200, 201) else ("Duplicate/Already Moved" if s_f == 422 else ("Funnel State Conflicts" if s_f == 400 else str(s_f)))
                            
                            s1, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data={"id": res["dp_id"], "companyName": {"value": res["company_name"]}, "description": {"value": res["ld"]}, "shortDescription": {"value": res["sd"]}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}}, headers=HEADERS)
                            ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": ["5dc5863a2799a51cc0ff30e2"], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                            sdld = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                            fun = "Done" if ms in (200, 201) else ("Duplicate/Already Moved" if ms == 422 else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        else:
                            s1, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data={"id": res["dp_id"], "companyName": {"value": res["company_name"]}, "description": {"value": res["ld"]}, "shortDescription": {"value": res["sd"]}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}}, headers=HEADERS)
                            ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": ["591d37b884ae06633a652496"], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                            sdld = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                            fun = "Sent discovery" if ms in (200, 201) else ("Duplicate/Already Moved" if ms == 422 else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        
                        await r_q.put({'range': f"K{idx}:N{idx}", 'values': [[feed_id, sdld, f_stat, fun]]})
                        await r_q.put({'type': 'progress', 'is_success': sdld in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts")})
                    else: await r_q.put({'type': 'progress', 'is_success': True})
                else:
                    reason = res.get('reason', 'Failed')
                    pipeline_logger.error(f"PIPELINE FAILED: {domain} | {reason}")
                    if self.mode != "phase2":
                        await r_q.put({'range': f"H{idx}", 'values': [[reason]]})
                    await r_q.put({'type': 'progress', 'is_success': False})
            except Exception as e:
                pipeline_logger.error(f"FATAL WORKER ERROR for {row[h_map['domain']] if row else 'Unknown'}: {e}")
                await r_q.put({'type': 'progress', 'is_success': False})
            finally:
                w_q.task_done()

    async def sheet_writer(self, r_q, ws, total, gc, pipeline_name):
        processed_indices, success_count, fail_count = set(), 0, 0
        batch_in, batch_out, batch_think, batch_rows = 0, 0, 0, 0
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
