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
from dotenv import load_dotenv

from sr_common.config import settings
from sr_common.clients import RateLimiter, GoogleSheetsClient
from sr_common.utils import call_gemini_api, call_tracxn_api, clean_html, extract_descriptions

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
    "MAX_WORKERS": 5,
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
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, 'TypeBPublishing.log'), mode="a"),
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

async def process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map) -> Dict:
    domain = row[h_map["domain"]]
    html, _ = await fetch_page(browser, f"https://{domain}")
    if not html: return {"type": "error", "reason": "Fetch failed"}
    body = clean_html(html)
    logging.info(f"Scraped {domain}: {len(body)} chars")
    if len(body) < 100: return {"type": "error", "reason": "Low content"}
    p1 = prompts[0].replace("XX", body[:20000])
    res_p1_obj = await call_gemini_api(session, p1, gemini_limiter)
    res_p1 = res_p1_obj.text
    in1, out1 = res_p1_obj.prompt_tokens, res_p1_obj.candidate_tokens
    sd, ld = extract_descriptions(res_p1)
    if not sd or not ld: return {"type": "error", "reason": "LLM failed"}
    
    feed = row[h_map["feed"]].split(" : ")[1] if " : " in row[h_map["feed"]] else row[h_map["feed"]]
    f_id, f_def = f_ids.get(feed, ""), f_defs.get(feed, "")
    
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
        gc = await GoogleSheetsClient.get_manager(self.config["CREDENTIALS_FILE"]).authorize()
        sheet = await gc.open_by_key(self.config["SHEET_ID"])
        ws = await sheet.worksheet(self.config["EXTRACTING_SHEET_NAME"])
        all_rows = await ws.get_all_values()
        data_rows = [r for r in all_rows[self.start_row-1:] if len(r) > 1 and r[1].strip() and r[1].strip() not in ["TypeA", "TypeB", "TypeC"]]
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
        paths = [[c for c in r if c.strip()] for r in (await (await sheet.worksheet("Paths")).get_all_values()) if any(r)]

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
                async with AsyncCamoufox(headless=True) as browser:
                    tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, browser, session, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
                    writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows)))
                    await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()

    async def domain_worker(self, w_q, r_q, browser, session, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map):
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
                            "type": "success", "dp_id": dp_id, "funnel_id": funnel_id, "sd": sd, "ld": ld,
                            "feed_id": row[15], "feedcheck": row[10], "bm_res": row[11], "bm_name": row[12], "bm_id": row[13],
                            "hashtags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
                            "tokens": {"in":0, "out":0}, "body_len": int(scrap_stat.split(":")[-1]) if ":" in scrap_stat else 0
                        }
                else: res = await process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_paths, bm_map, f_defs, h_map)
                
                if res["type"] == "success":
                    if self.mode != "phase2":
                        await r_q.put({'range': f"H{idx}:N{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld"], res["feedcheck"], res["bm_res"], res["bm_name"], res["bm_id"]]]})
                        await r_q.put({'range': f"T{idx}:U{idx}", 'values': [[res["tokens"]["in"], res["tokens"]["out"]]]})
                    
                    if self.mode != "phase1":
                        tags = res["hashtags"] + ["bu_llm_sd_ld", "llmbasedpublishing"]
                        if res["feedcheck"] == "Yes": tags.append("bu_llm_businessmodel_prediction")
                        sdld, bm, fun = "N/A", "N/A", "N/A"
                        f_id = res["feed_id"] or f_ids.get(res["bm_name"])
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
                        await r_q.put({'range': f"O{idx}:S{idx}", 'values': [["N/A", f_id, sdld, bm, fun]]})
                        await r_q.put({'type': 'progress', 'is_success': sdld in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts")})
                    else: await r_q.put({'type': 'progress', 'is_success': True})
                else:
                    if self.mode != "phase2":
                        await r_q.put({'range': f"H{idx}:N{idx}", 'values': [["No", "Failed", res.get("reason", ""), "", "", "", ""]]})
                    await r_q.put({'type': 'progress', 'is_success': False})
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
                    logging.error(f"SHEET WRITER ERR: {e}")
                finally:
                    for _ in updates: r_q.task_done()
                    self.report_progress(len(processed_indices), total, success, fail)
                    logging.info(f"PROGRESS: {len(processed_indices)}/{total} | Success: {success} | Fail: {fail}")
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
