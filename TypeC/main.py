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
from dotenv import load_dotenv

from sr_common.config import settings
from sr_common.clients import RateLimiter, GoogleSheetsClient
from sr_common.utils import call_gemini_api, call_tracxn_api, clean_html, extract_descriptions

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
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, 'TypeCPublishing.log'), mode="a"),
        logging.StreamHandler()
    ]
)

gemini_limiter = RateLimiter(2000)
tracxn_limiter = RateLimiter(160)

async def fetch_page(browser, url: str) -> Tuple[Optional[str], int]:
    try:
        page = await browser.new_page()
        response = await page.goto(url, wait_until="load", timeout=CONFIG["REQUEST_TIMEOUT"]*1000)
        await asyncio.sleep(2)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        content = await page.content()
        status = response.status if response else 0
        await page.close()
        return content, status
    except: return None, 0

async def process_domain_stage1(browser, session, row, prompts, f_ids, h_map) -> Dict:
    domain = row[h_map["domain"]]
    dp_id, funnel_id, hashtags = row[h_map["dp_id"]], row[h_map["funnel_id"]], [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else []
    
    html, _ = await fetch_page(browser, f"https://{domain}")
    if not html: return {"type": "error", "reason": "Fetch failed"}
    body = clean_html(html)
    logging.info(f"Scraped {domain}: {len(body)} chars")
    if len(body) < 100: return {"type": "error", "reason": "Low content"}
    p1 = prompts[0].replace("XX", body[:20000])
    res_obj = await call_gemini_api(session, p1, gemini_limiter)
    res, in_p, out_p = res_obj.text, res_obj.prompt_tokens, res_obj.candidate_tokens
    
    sd_match = re.search(r'Short Description:\s*(.*)', res)
    ld_match = re.search(r'Long Description:\s*(.*)', res)
    sd = sd_match.group(1).strip() if sd_match else res[:100].strip()
    ld = ld_match.group(1).strip() if ld_match else res.strip()
    
    if not sd or not ld or ld == "Error": return {"type": "error", "reason": "LLM failed"}

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
        gc = await GoogleSheetsClient.get_manager(self.config["CREDENTIALS_FILE"]).authorize()
        sheet = await gc.open_by_key(self.config["SHEET_ID"])
        ws = await sheet.worksheet(self.config["EXTRACTING_SHEET_NAME"])
        all_rows = await ws.get_all_values()
        h_map = {
            "domain": 1, "dp_id": 2, "funnel_id": 4, "tags": 5, "company_name": 6,
            "skip": 7, "sd": 9, "ld": 10, "feed_id": 11, "funnel_name": 3
        }
        all_rows = await ws.get_all_values()
        data_rows = [r for r in all_rows[self.start_row-1:] if len(r) > 1 and r[1].strip()]
        
        p_sheet = await gc.open_by_key(self.config["PROMPTS_SHEET_ID"])
        prompts = [r[1] for r in (await (await p_sheet.worksheet("Prompts")).get_all_values())[1:10]]
        fo_sheet = await gc.open_by_key(self.config["FEED_OWNER_SHEET_ID"])
        f_ids = {r[0]: r[1] for r in (await (await fo_sheet.worksheet("Feed Owner Details")).get_all_values())}

        work_queue, result_queue = asyncio.Queue(), asyncio.Queue()
        for idx, row in enumerate(data_rows, start=self.start_row):
            if len(row) > h_map["skip"] and row[h_map["skip"]] == "Yes": continue
            await work_queue.put((idx, row))

        async with aiohttp.ClientSession() as session:
            if self.mode == "phase2":
                tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, None, session, prompts, f_ids, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
                writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows)))
                await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()
            else:
                async with AsyncCamoufox(headless=True) as browser:
                    tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, browser, session, prompts, f_ids, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
                    writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows)))
                    await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()

    async def domain_worker(self, w_q, r_q, browser, session, prompts, f_ids, h_map):
        while True:
            idx, row = await w_q.get()
            try:
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
                    await r_q.put({'range': f"H{idx}:J{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld"]]]})
                    
                    if self.mode != "phase1":
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
                    await r_q.put({'range': f"H{idx}:J{idx}", 'values': [["No", "Failed", res.get("reason", "Unknown")]]})
                    await r_q.put({'type': 'progress', 'is_success': False})
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
                    logging.error(f"SHEET WRITER ERR: {e}")
                finally:
                    for _ in updates: r_q.task_done()
                    self.report_progress(len(processed_indices), total, success_count, fail_count)
                    logging.info(f"PROGRESS: {len(processed_indices)}/{total} | Success: {success_count} | Fail: {fail_count}")
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
