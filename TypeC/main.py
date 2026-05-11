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
from sr_common.utils import call_gemini_api, call_tracxn_api, clean_html, extract_descriptions, is_parked_domain

# Configuration
CONFIG = {
    "SHEET_ID": settings.TYPEC_SHEET_ID,
    "PROMPTS_SHEET_ID": settings.PROMPTS_SHEET_ID,
    "FEED_OWNER_SHEET_ID": settings.FEED_OWNER_SHEET_ID,
    "EXTRACTING_SHEET_NAME": "DB",
    "CREDENTIALS_FILE": os.path.join(os.path.dirname(os.path.abspath(__file__)), "TypeC.json"),
    "MAX_WORKERS": 5,
    "GEMINI_API_URL": settings.GEMINI_API_URL,
    "GEMINI_API_KEY": settings.TYPEC_GEMINI_API_KEY,
    "MAX_PROMPT_SIZE": settings.MAX_PROMPT_SIZE,
    "BATCH_SIZE": 10,
    "REQUEST_TIMEOUT": 60,
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
        s_fetcher = Fetcher()
        s_resp = s_fetcher.get(url, timeout=60, verify=False)
        
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
    try:
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
                await context.close()
                return content, 200, "Success"
            await context.close()
            scrap_logger.warning(f"TIER 2 FAIL: {url} | Reason: Captcha or Low Content")
        else:
            await context.close()
            scrap_logger.warning(f"TIER 2 FAIL: {url} | Status: {response.status if response else 'No Resp'}")
    except Exception as e:
        scrap_logger.warning(f"TIER 2 ERR: {url} | {str(e)}")

    # --- TIER 3: SCRAPLING STEALTH ---
    try:
        from scrapling import StealthyFetcher
        scrap_logger.info(f"TIER 3: Scrapling Stealth (Playwright) for {url}")
        sf = StealthyFetcher(headless=True)
        s_resp = await sf.async_fetch(url, timeout=60, verify=False)
        
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

async def process_domain_stage1(browser, session, row, prompts, f_ids, h_map) -> Dict:
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
    res_obj = await call_gemini_api(session, p1, gemini_limiter)
    res, in_p, out_p = res_obj.text, res_obj.prompt_tokens, res_obj.candidate_tokens
    
    sd, ld = extract_descriptions(res)
    if sd == "NO_DATA":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Insufficient content (AI reported NO_DATA)")
        return {"type": "error", "reason": "Low content"}
    if sd == "PARKED_LLM":
        pipeline_logger.warning(f"PROCESS FAILED: {domain} | Reason: Parked (AI reported PARKED_LLM)")
        return {"type": "error", "reason": "Parked"}
    
    if not sd or not ld: 
        pipeline_logger.error(f"PROCESS FAILED: {domain} | Reason: LLM failed to generate descriptions")
        return {"type": "error", "reason": "LLM failed"}

    pipeline_logger.info(f"PROCESS SUCCESS: {domain}")
    return {
        "type": "success", "sd": sd, "ld": ld[:40000], "tokens": {"in": in_p, "out": out_p}, 
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
        # Start system monitoring
        asyncio.create_task(log_system_metrics())
        gc = await GoogleSheetsClient.get_manager(self.config["CREDENTIALS_FILE"]).authorize()
        sheet = await gc.open_by_key(self.config["SHEET_ID"])
        ws = await sheet.worksheet(self.config["EXTRACTING_SHEET_NAME"])
        all_rows = await ws.get_all_values()
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
            # No explicit skip column in Type C currently
            await work_queue.put((idx, row))

        async with aiohttp.ClientSession() as session:
            if self.mode == "phase2":
                tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, None, session, prompts, f_ids, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
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
                    tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, browser, session, prompts, f_ids, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
                    writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows)))
                    await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()

    async def domain_worker(self, w_q, r_q, browser, session, prompts, f_ids, h_map):
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
                            "type": "success", "sd": sd, "ld": ld, "dp_id": dp_id, "funnel_id": funnel_id,
                            "funnel_name": row[h_map["funnel_name"]], "company_name": row[h_map["company_name"]],
                            "tags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
                            "tokens": {"in":0, "out":0}, "body_len": int(scrap_stat.split(":")[-1]) if ":" in scrap_stat else 0
                        }
                else: res = await process_domain_stage1(browser, session, row, prompts, f_ids, h_map)
                
                if res["type"] == "success":
                    if self.mode != "phase2":
                        await r_q.put({'range': f"H{idx}:J{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld"]]]})
                        await r_q.put({'range': f"O{idx}:P{idx}", 'values': [[res["tokens"]["in"], res["tokens"]["out"]]]})
                    
                    if self.mode != "phase1":
                        pipeline_logger.info(f"PIPELINE: Updating Tracxn for {domain}")
                        tags = res["tags"] + ["bu_llm_typec_autopublish"]
                        funnel_name = res["funnel_name"]
                        feed = funnel_name.split(" : ")[1] if " : " in funnel_name else funnel_name
                        feed_id = f_ids.get(feed, "")
                        
                        f_stat, sdld, fun = "N/A", "N/A", "N/A"
                        if feed_id:
                            s_f, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/3.0/w/theme-company-association", tracxn_limiter, method="put", json_data={"object": {"themeId": feed_id, "status": "PUBLISHED", "companyId": res["dp_id"]}, "opType": "Update"}, headers=HEADERS)
                            f_stat = "Done" if s_f in (200, 201) else ("Duplicate/Already Moved" if s_f == 422 else ("Funnel State Conflicts" if s_f == 400 else str(s_f)))
                            
                            s1, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data={"id": res["dp_id"], "description": {"value": res["ld"]}, "shortDescription": {"value": res["sd"]}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}}, headers=HEADERS)
                            ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": ["5dc5863a2799a51cc0ff30e2"], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                            sdld = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                            fun = "Done" if ms in (200, 201) else ("Duplicate/Already Moved" if ms == 422 else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        else:
                            s1, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, method="put", json_data={"id": res["dp_id"], "description": {"value": res["ld"]}, "shortDescription": {"value": res["sd"]}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}}, headers=HEADERS)
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
                pipeline_logger.error(f"PIPELINE EXC: {str(e)}")
            finally: w_q.task_done()

    async def sheet_writer(self, r_q, ws, total):
        processed_indices, success_count, fail_count = set(), 0, 0
        while True:
            updates = []
            while not r_q.empty() and len(updates) < CONFIG["BATCH_SIZE"]:
                item = await r_q.get()
                if isinstance(item, dict) and item.get('type') == 'progress':
                    if item.get('is_success'): success_count += 1
                    else: fail_count += 1
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
                    self.report_progress(len(processed_indices), total, success_count, fail_count)
                    pipeline_logger.info(f"PROGRESS: {len(processed_indices)}/{total} | Success: {success_count} | Fail: {fail_count}")
            else: self.report_progress(len(processed_indices), total, success_count, fail_count)
            await asyncio.sleep(1)

    def report_progress(self, curr, total, success, fail):
        try:
            with open(".progress.json", "w") as f: json.dump({"current": curr, "total": total, "success": success, "fail": fail}, f)
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
        
    pipeline = TypeCPipeline(row, mode)
    if sheet_id:
        pipeline.config["SHEET_ID"] = sheet_id
    pipeline.apply_formatting = apply_formatting
    await pipeline.run()

if __name__ == "__main__": asyncio.run(main())
