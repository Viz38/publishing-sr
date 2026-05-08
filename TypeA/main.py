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
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

from sr_common.config import settings
from sr_common.clients import RateLimiter, GoogleSheetsClient
from sr_common.utils import call_gemini_api, call_tracxn_api, clean_html, extract_descriptions

# Configuration
CONFIG = {
    "SHEET_ID": settings.TYPEA_SHEET_ID,
    "MASTER_SHEET_ID": settings.MASTER_SHEET_ID,
    "PROMPTS_SHEET_ID": settings.PROMPTS_SHEET_ID,
    "FEED_OWNER_SHEET_ID": settings.FEED_OWNER_SHEET_ID,
    "FEED_DEF_SHEET_ID_1": settings.FEED_DEF_SHEET_ID_1,
    "FEED_DEF_SHEET_ID_2": settings.FEED_DEF_SHEET_ID_2,
    "EXTRACTING_SHEET_NAME": "DB",
    "CREDENTIALS_FILE": os.path.join(os.path.dirname(os.path.abspath(__file__)), "TypeA.json"),
    "MAX_WORKERS": 5,
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
        logging.FileHandler(os.path.join(LOGS_DIR, 'TypeAPublishing.log'), mode="a"),
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

def extract_links(html: str, base_url: str) -> Set[str]:
    if not html: return set()
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    domain = urlparse(base_url).netloc
    for a in soup.find_all('a', href=True):
        url = urljoin(base_url, a['href'])
        if urlparse(url).netloc == domain:
            links.add(url.split('#')[0].rstrip('/'))
    return links

async def process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map) -> Dict:
    domain = row[h_map["domain"]]
    dp_id, funnel_id, hashtags = row[h_map["dp_id"]], row[h_map["funnel_id"]], [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else []
    
    html, _ = await fetch_page(browser, f"https://{domain}")
    if not html: return {"type": "error", "reason": "Home fetch failed"}
    
    body_results = [clean_html(html)]
    links = extract_links(html, f"https://{domain}")
    
    target_urls = []
    scraped_urls = {f"https://{domain}"}
    for group in paths:
        for p in group:
            m = next((l for l in links if p in l and l not in scraped_urls), None)
            if m: target_urls.append(m); scraped_urls.add(m); break
    
    if target_urls:
        res = await asyncio.gather(*[fetch_page(browser, u) for u in target_urls])
        body_results.extend([clean_html(r[0]) for r in res if r[0]])
    
    combined = "\n\n".join(body_results)
    p1 = prompts[0].replace("XX", combined[:CONFIG["MAX_PROMPT_SIZE"]])
    res_p1_obj = await call_gemini_api(session, p1, gemini_limiter)
    res_p1 = res_p1_obj.text
    in1, out1 = res_p1_obj.prompt_tokens, res_p1_obj.candidate_tokens
    sd, ld1 = extract_descriptions(res_p1)
    
    p2 = prompts[1].replace("XX", combined[:CONFIG["MAX_PROMPT_SIZE"]]).replace("YY", sd)
    res_p2_obj = await call_gemini_api(session, p2, gemini_limiter)
    res_p2 = res_p2_obj.text
    in2, out2 = res_p2_obj.prompt_tokens, res_p2_obj.candidate_tokens
    _, ld2 = extract_descriptions(res_p2)
    
    ld_main = f"{ld1}\n\n{ld2}"
    if not sd or not ld1: return {"type": "error", "reason": "LLM failed"}

    feed = row[h_map["feed"]].split(" : ")[1] if " : " in row[h_map["feed"]] else row[h_map["feed"]]
    f_id, f_def = f_ids.get(feed, ""), f_defs.get(feed, "")
    
    # BM Logic
    bm_p1 = prompts[6].replace("YY", ld_main).replace("XX", f_def).replace("BM_Paths", "\n".join([f"{r[0]}. {r[1]} - {r[2]}" for r in bm_mapping.get(feed, {}).get("1stLevel", [])]))
    res_bm1_obj = await call_gemini_api(session, bm_p1, gemini_limiter)
    res_bm1 = res_bm1_obj.text
    in3, out3 = res_bm1_obj.prompt_tokens, res_bm1_obj.candidate_tokens
    bm_name_1 = "No Results"
    m = re.search(r'^\d+[\.\s]+\s*(.*?)\s*[,:-]\s*Explanation', res_bm1, re.M)
    if m: bm_name_1 = m.group(1).strip()
    
    bm_name_2, bm_id_2, res_bm2, bm_p2, in4, out4 = "No BM matched", "No ID", "", "", 0, 0
    f_bms2 = bm_mapping.get(feed, {}).get("2ndLevel", [])
    if bm_name_1 != "No Results" and f_bms2:
        filt = [s for s in f_bms2 if s[2].startswith(bm_name_1)]
        if filt:
            bm_p2 = prompts[7].replace("XX", ld_main).replace("BM_Paths", "\n".join([" ".join(map(str, r)) for r in filt]))
            res_bm2_obj = await call_gemini_api(session, bm_p2, gemini_limiter)
            res_bm2 = res_bm2_obj.text
            in4, out4 = res_bm2_obj.prompt_tokens, res_bm2_obj.candidate_tokens
            m2 = re.search(r'^\d+[\.\s]+\s*(.*?)\s*[,:-]\s*Explanation', res_bm2, re.M)
            if m2: bm_name_2 = m2.group(1).strip(); bm_id_2 = bm_ids.get(bm_name_2, "No ID")
            elif bm_1st_stat.get(bm_name_1) == "Live": bm_name_2, bm_id_2 = bm_name_1, bm_ids.get(bm_name_1, "No ID")

    return {
        "type": "success", "dp_id": dp_id, "funnel_id": funnel_id, "hashtags": hashtags,
        "sd": sd, "ld1": ld1, "ld2": ld2, "bmp1": bm_p1[:40000], "bmr1": res_bm1[:40000], "bmp2": bm_p2[:40000], "bmr2": res_bm2[:40000], "bm_name": bm_name_2, "bm_id": bm_id_2, "sf": ", ".join(hashtags), "feed_id": f_id,
        "tokens": {"in": in1+in2+in3+in4, "out": out1+out2+out3+out4}, "body_len": len(combined)
    }

class TypeAPipeline:
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
        data_rows = [r for r in all_rows[self.start_row-1:] if len(r) > 1 and r[1].strip()]
        h_map = {
            "domain": 1, "dp_id": 2, "feed": 3, "funnel_id": 4, "tags": 5,
            "skip": 7, "sd": 9, "ld1": 10, "ld2": 11, "feed_id": 19
        }
        
        m_sheet = await gc.open_by_key(self.config["MASTER_SHEET_ID"])
        f_lvl = await (await m_sheet.worksheet("1st Level")).get_all_values()
        s_lvl = await (await m_sheet.worksheet("2nd Level Live BM's")).get_all_values()
        bm_mapping, bm_ids, bm_1st_stat = {}, {}, {}
        for r in f_lvl[1:]:
            if len(r) < 5: continue
            f, p, bid, stat, desc = r[0], r[1], r[2], r[3], r[4]
            bm_ids[p], bm_1st_stat[p] = bid, stat
            if f not in bm_mapping: bm_mapping[f] = {"1stLevel":[], "2ndLevel":[]}
            bm_mapping[f]["1stLevel"].append([len(bm_mapping[f]["1stLevel"])+1, p, desc])
        for r in s_lvl[1:]:
            if len(r) < 4: continue
            f, p, bid, desc = r[0], r[1], r[2], r[3]
            bm_ids[p] = bid
            if f not in bm_mapping: bm_mapping[f] = {"1stLevel":[], "2ndLevel":[]}
            bm_mapping[f]["2ndLevel"].append([len(bm_mapping[f]["2ndLevel"])+1, ".", p, " -"+desc])

        prompts = [r[1] for r in (await (await (await gc.open_by_key(CONFIG["PROMPTS_SHEET_ID"])).worksheet("Prompts")).get_all_values())[1:10]]
        f_ids = {r[0]: r[1] for r in (await (await (await gc.open_by_key(CONFIG["FEED_OWNER_SHEET_ID"])).worksheet("Feed Owner Details")).get_all_values())}
        f_defs = {}
        for sid in [CONFIG["FEED_DEF_SHEET_ID_1"], CONFIG["FEED_DEF_SHEET_ID_2"]]:
            try:
                fd_data = await (await (await gc.open_by_key(sid)).worksheet("Feed Definition")).get_all_values()
                for r in fd_data[1:]:
                    if len(r) > 3: f_defs[r[1]] = r[3]
            except: pass
        paths = [[c for c in r if c.strip()] for r in (await (await sheet.worksheet("Paths")).get_all_values()) if any(r)]

        work_queue, result_queue = asyncio.Queue(), asyncio.Queue()
        for idx, row in enumerate(data_rows, start=self.start_row):
            if len(row) > h_map["skip"] and row[h_map["skip"]] == "Yes": continue
            await work_queue.put((idx, row))

        async with aiohttp.ClientSession() as session:
            if self.mode == "phase2":
                tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, None, session, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
                writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows)))
                await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()
            else:
                async with AsyncCamoufox(headless=True) as browser:
                    tasks = [asyncio.create_task(self.domain_worker(work_queue, result_queue, browser, session, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map)) for _ in range(CONFIG["MAX_WORKERS"])]
                    writer_task = asyncio.create_task(self.sheet_writer(result_queue, ws, len(data_rows)))
                    await work_queue.join(); [t.cancel() for t in tasks]; await result_queue.join(); writer_task.cancel()

    async def domain_worker(self, w_q, r_q, browser, session, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map):
        while True:
            idx, row = await w_q.get()
            try:
                date_str = datetime.now().strftime("%d-%b-%Y")
                await r_q.put({'range': f"A{idx}", 'values': [[date_str]]})
                if self.mode == "phase2":
                    sd, ld1, dp_id, funnel_id = row[h_map["sd"]], row[h_map["ld1"]], row[h_map["dp_id"]], row[h_map["funnel_id"]]
                    scrap_stat = row[h_map["domain"] + 7] if len(row) > 8 else ""
                    if not (sd and ld1 and dp_id and funnel_id and scrap_stat.startswith("Yes")):
                        res = {"type": "failed", "reason": "Missing Phase 1 inputs"}
                    else:
                        res = {
                            "type": "success", "dp_id": dp_id, "funnel_id": funnel_id, "sd": sd, "ld1": ld1,
                            "ld2": row[11], "bmp1": row[12], "bmr1": row[13], "bmp2": row[14], "bmr2": row[15],
                            "bm_name": row[16], "bm_id": row[17], "sf": row[18], "feed_id": row[19],
                            "hashtags": [t.strip() for t in row[h_map["tags"]].split(",")] if row[h_map["tags"]] else [],
                            "tokens": {"in":0, "out":0}, "body_len": int(scrap_stat.split(":")[-1]) if ":" in scrap_stat else 0
                        }
                else: res = await process_domain_stage1(browser, session, row, prompts, paths, f_ids, bm_mapping, f_defs, bm_ids, bm_1st_stat, h_map)
                
                if res["type"] == "success":
                    if self.mode != "phase2":
                        await r_q.put({'range': f"I{idx}:T{idx}", 'values': [[f"Yes: {res.get('body_len', 0)}", res["sd"], res["ld1"], res["ld2"], res["bmp1"], res["bmr1"], res["bmp2"], res["bmr2"], res["bm_name"], res["bm_id"], res["sf"], res["feed_id"]]]})
                        await r_q.put({'range': f"X{idx}:Y{idx}", 'values': [[res["tokens"]["in"], res["tokens"]["out"]]]})
                    
                    if self.mode != "phase1":
                        tags = res["hashtags"] + ["bu_llm_sd_ld", "bu_Internal_SRprocess_TypeA"]
                        payload = {"id": res["dp_id"], "description": {"value": res["ld1"]}, "shortDescription": {"value": res["sd"]}, "keywords": {"value": {"HASHTAGS": tags}}, "publishingDepth": {"value": "Pub 2 - Partial"}}
                        s1, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/2.0/domain-profile", tracxn_limiter, json_data=payload, headers=HEADERS)
                        edits = "Done" if s1 in (200, 201) else ("Duplicate/Already Moved" if s1 == 422 else ("Funnel State Conflicts" if s1 == 400 else f"Err {s1}"))
                        
                        bm_up, fun_up = "N/A", "N/A"
                        f_name = row[h_map["feed"]]
                        f_id = res["feed_id"] or f_ids.get(res["bm_name"]) or f_ids.get(f_name)
                        if edits in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts") and res["bm_id"] != "No ID":
                            if f_id:
                                s2, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/entities/3.0/w/theme-company-association", tracxn_limiter, json_data={"object": {"themeId": f_id, "status": "PUBLISHED", "businessModelId": res["bm_id"], "companyId": res["dp_id"]}, "opType": "Update"}, headers=HEADERS)
                                bm_up = "Done" if s2 in (200, 201) else ("Duplicate/Already Moved" if s2 == 422 else ("Funnel State Conflicts" if s2 == 400 else str(s2)))
                                
                                if bm_up in ("Done", "Duplicate/Already Moved", "Funnel State Conflicts"):
                                    ms, _ = await call_tracxn_api(session, "https://platform.tracxn.com/data/funnel-action/move", tracxn_limiter, method="put", json_data={"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": ["5dc5863a2799a51cc0ff30e2"], "sourceDetails": {"source": "Write API"}}, headers=HEADERS)
                                    fun_up = "Done" if ms in (200, 201) else ("Duplicate/Already Moved" if ms == 422 else ("Funnel State Conflicts" if ms == 400 else "Err"))
                        await r_q.put({'range': f"T{idx}:W{idx}", 'values': [[f_id, edits, bm_up, fun_up]]})
                        await r_q.put({'type': 'progress', 'is_success': (edits == "Done")})
                    else: await r_q.put({'type': 'progress', 'is_success': True})
                else:
                    if self.mode != "phase2":
                        await r_q.put({'range': f"I{idx}", 'values': [["Failed"]]})
                    await r_q.put({'type': 'progress', 'is_success': False})
            finally: w_q.task_done()

    async def sheet_writer(self, r_q, ws, total):
        processed, s, f = set(), 0, 0
        while True:
            updates = []
            while not r_q.empty() and len(updates) < CONFIG["BATCH_SIZE"]:
                item = await r_q.get()
                if isinstance(item, dict) and item.get('type') == 'progress':
                    if item.get('is_success'): s += 1
                    else: f += 1
                    r_q.task_done(); continue
                updates.append(item)
            if updates:
                try:
                    await ws.batch_update(updates, value_input_option='USER_ENTERED')
                    for u in updates:
                        m = re.search(r'\d+', u['range'])
                        if m: processed.add(int(m.group()))
                except Exception as e:
                    logging.error(f"Error while calling batch_update: {e}")
                finally:
                    for _ in updates: r_q.task_done()
                    self.report_progress(len(processed), total, s, f)
            else: self.report_progress(len(processed), total, s, f)
            await asyncio.sleep(1)

    def report_progress(self, curr, total, s, f):
        try:
            with open(".progress.json", "w") as file: json.dump({"current": curr, "total": total, "success": s, "fail": f}, file)
        except: pass

async def main():
    import sys
    row = int(sys.argv[1]) if len(sys.argv) > 1 else 10
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

if __name__ == "__main__": asyncio.run(main())
