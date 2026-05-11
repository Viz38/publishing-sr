from python import Python

def run_pipeline(start_row: Int, mode: String, sheet_id: String, apply_formatting: Bool) raises:
    var asyncio = Python.import_module("asyncio")
    print("🚀 Mojo: Starting Type A Pipeline (Modern Engine)...")

    var python_code = r"""
import asyncio
import aiohttp
import logging
import json
import re
import os
import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials
from bs4 import BeautifulSoup
from gspread_formatting import *

CONFIG = {
    "SHEET_ID": "1vAl9LeXrguCMxMjJtqWpX1SmcKOUSKRmO-46O_IeTtk",
    "MASTER_SHEET_ID": "1hi_Zb_0DsK8CRqWST3_FrrjG3tj5NoMKrZHUroYLcOw",
    "PROMPTS_SHEET_ID": "1N9GgEXIiR7QwEpzpJCvGbXlZ_kCgN9ev8fj0Ynv98MU",
    "FEED_OWNER_SHEET_ID": "1VSvvKsjO5ZPSg3ff6SnwPEQ0i9BTwzAI-aCiWxjHzYU",
    "FEED_DEF_SHEET_ID": "1HEmWY4AeFltmjPbMzX-xDydTsncMX53hpbgHpsS_-44",
    "EXTRACTING_SHEET_NAME": "DB",
    "CREDENTIALS_FILE": "TypeA.json",
    "MAX_WORKERS": 100,
    "GEMINI_API_URL": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent",
    "GEMINI_API_KEY": "AIzaSyArzB6lNcYALn5tsxzsOXpSRKbhzPTdLpM",
    "REQUEST_TIMEOUT": 45,
    "MAX_RETRIES": 3,
    "RETRY_DELAY": 5, 
    "BATCH_SIZE": 20
}

HEADERS = {
    "accesstoken": "efa37d29-008f-43ad-a21b-44fd0443c462",
    "Content-Type": "application/json",
    "X-Request-Source": 'Type-A-Publishing'
}

MAIN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
}

class GoogleSheetsClient:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_name(CONFIG["CREDENTIALS_FILE"], scope)
            cls._instance = gspread_asyncio.AsyncioGspreadClientManager(lambda: creds)
        return cls._instance

async def call_tracxn_api(session, url, method="put", json_data=None):
    attempt = 0
    while True:
        try:
            async with session.request(method, url, json=json_data, headers=HEADERS) as response:
                if response.status in (200, 201, 422): return response.status, await response.json()
                await asyncio.sleep(min(2 * (2 ** attempt), 60))
                attempt += 1
        except:
            await asyncio.sleep(min(2 * (2 ** attempt), 60))
            attempt += 1

async def process_domain_stage1(session, r_data, prompts, match_paths, feed_id_map, bm_mapping, bm_ids, feed_def_map, bm_1st_stat):
    domain = r_data[1]
    url = f"https://{domain}"
    try:
        async with session.get(url, timeout=15, headers=MAIN_HEADERS) as resp:
            html = await resp.text()
            body = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL)
            body = re.sub(r'<[^>]+>', '', body)
            body = " ".join(body.split())[:30000]
            
            prompt = prompts[0].replace("XX", body)
            async with session.post(f"{CONFIG['GEMINI_API_URL']}?key={CONFIG['GEMINI_API_KEY']}", json={"contents":[{"parts":[{"text":prompt}]}]}) as g_resp:
                res_json = await g_resp.json()
                text = res_json['candidates'][0]['content']['parts'][0]['text']
                sd = re.search(r'Short Description:\s*(.*)', text).group(1)
                ld = re.search(r'Long Description:\s*(.*)', text).group(1)
                
                feed = r_data[3].split(" : ")[1]
                return {
                    "type": "success", "domain": domain, "dp_id": r_data[2], "funnel_id": r_data[4],
                    "hashtags": [t.strip() for t in r_data[5].split(",")] if r_data[5] else [],
                    "sd": sd, "ld1": ld, "bm_id": "No ID", "feed_id": feed_id_map.get(feed, ""),
                    "tokens": {"in": res_json.get('usageMetadata',{}).get('promptTokenCount',0), "out": res_json.get('usageMetadata',{}).get('candidatesTokenCount',0)}
                }
    except: return {"type": "error"}

async def corrected_run_typea(start_row, mode, sheet_id=None, apply_formatting=True):
    client = GoogleSheetsClient()
    gc = await client.authorize()
    sheet = await gc.open_by_key(sheet_id or CONFIG["SHEET_ID"])
    ws = await sheet.worksheet(CONFIG["EXTRACTING_SHEET_NAME"])
    
    all_rows = await ws.get_all_values()
    data_rows = all_rows[start_row-1:]
    
    m_sheet = await gc.open_by_key(CONFIG["MASTER_SHEET_ID"])
    p_sheet = await gc.open_by_key(CONFIG["PROMPTS_SHEET_ID"])
    prompts = [r[1] for r in (await (await p_sheet.worksheet("Prompts")).get_all_values())[1:10]]
    
    fo_sheet = await gc.open_by_key(CONFIG["FEED_OWNER_SHEET_ID"])
    feed_id_map = {r[0]: r[1] for r in (await (await fo_sheet.worksheet("Feed Owner Details")).get_all_values())}
    
    semaphore = asyncio.Semaphore(CONFIG["MAX_WORKERS"])
    async with aiohttp.ClientSession() as session:
        def report_progress(current, total, success):
            try:
                with open(".progress.json", "w") as f:
                    json.dump({"current": current, "total": total, "success": success}, f)
            except: pass

        total_rows = len(data_rows)
        successful_count = 0
        current_count = 0
        report_progress(0, total_rows, 0)

        for i in range(0, len(data_rows), CONFIG["BATCH_SIZE"]):
            batch = data_rows[i:i+CONFIG["BATCH_SIZE"]]
            tasks = []
            for idx, row in enumerate(batch, start=start_row + i):
                if not row or len(row) < 6: 
                    current_count += 1
                    continue
                async def worker(r_data, r_idx):
                    nonlocal successful_count, current_count
                    try:
                        async with semaphore:
                            res = await process_domain_stage1(session, r_data, prompts, [], feed_id_map, {}, {}, {}, {})
                            if res["type"] == "success":
                                successful_count += 1
                                if mode in ["full", "phase2"]:
                                    p_url = "https://platform.tracxn.com/data/entities/2.0/domain-profile"
                                    p_payload = {"id": res["dp_id"], "description":{"value":res["ld1"]}, "shortDescription":{"value":res["sd"]}, "keywords":{"value":{"HASHTAGS":res["hashtags"] + ["bu_llm_sd_ld", "bu_Internal_SRprocess_TypeA"]}}, "publishingDepth":{"value":"Pub 1 - Full"}}
                                    await call_tracxn_api(session, p_url, json_data=p_payload)
                                    m_url = "https://platform.tracxn.com/data/funnel-action/move"
                                    m_payload = {"funnelId": res["funnel_id"], "domainProfileId": res["dp_id"], "movedTo": ["5dc5863a2799a51cc0ff30e2"], "sourceDetails": {"source": "Write API", "sourceData": {"view": "Card", "tab": "Funnel Homepage"}}}
                                    await call_tracxn_api(session, m_url, json_data=m_payload)
                                s_data = ["Yes", res["sd"], res["ld1"], "", "", "", "", "", "", "", "", res["feed_id"], "Done", "N/A", "Done", res["tokens"]["in"], res["tokens"]["out"]]
                                await ws.update(f"I{r_idx}:Y{r_idx}", [s_data], value_input_option='USER_ENTERED')
                    except: pass
                    finally:
                        current_count += 1
                        report_progress(current_count, total_rows, successful_count)
                tasks.append(worker(row, idx))
            await asyncio.gather(*tasks)
"""
    var py = Python.import_module("builtins")
    var globals = py.dict()
    _ = py.exec(python_code, globals)
    var corrected_func = globals["corrected_run_typea"]
    _ = asyncio.run(corrected_func(start_row, mode, sheet_id, apply_formatting))
    print("✅ Mojo: Type A Pipeline Completed.")

def main():
    try:
        var sys = Python.import_module("sys")
        var argv = sys.argv
        var start_row: Int = 3
        var mode: String = "full"
        var sheet_id: String = ""
        var apply_formatting: Bool = True
        
        if len(argv) > 1: start_row = atol(String(argv[1]))
        if len(argv) > 2: mode = String(argv[2])
        if len(argv) > 3: sheet_id = String(argv[3])
        if len(argv) > 4: apply_formatting = String(argv[4]).lower() == "true"
        
        _ = run_pipeline(start_row, mode, sheet_id, apply_formatting)
    except e:
        print("❌ Mojo Error:", e)
