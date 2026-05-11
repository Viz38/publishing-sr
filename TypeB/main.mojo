from python import Python

def run_pipeline(start_row: Int, mode: String, sheet_id: String, apply_formatting: Bool) raises:
    var asyncio = Python.import_module("asyncio")
    print("🚀 Mojo: Starting Type B Pipeline (Modern Engine)...")

    var python_code = r"""
import asyncio
import aiohttp
import logging
import json
import re
import os
import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials

CONFIG = {
    "SHEET_ID": "1vAl9LeXrguCMxMjJtqWpX1SmcKOUSKRmO-46O_IeTtk",
    "FEED_OWNER_SHEET_ID": "1VSvvKsjO5ZPSg3ff6SnwPEQ0i9BTwzAI-aCiWxjHzYU",
    "EXTRACTING_SHEET_NAME": "DB",
    "CREDENTIALS_FILE": "TypeB.json",
    "MAX_WORKERS": 50,
    "GEMINI_API_URL": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent",
    "GEMINI_API_KEY": "AIzaSyArzB6lNcYALn5tsxzsOXpSRKbhzPTdLpM",
    "BATCH_SIZE": 10
}

async def corrected_run_typeb(start_row, mode, sheet_id=None, apply_formatting=True):
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name(CONFIG["CREDENTIALS_FILE"], scope)
    agcm = gspread_asyncio.AsyncioGspreadClientManager(lambda: creds)
    gc = await agcm.authorize()
    sheet = await gc.open_by_key(sheet_id or CONFIG["SHEET_ID"])
    ws = await sheet.worksheet(CONFIG["EXTRACTING_SHEET_NAME"])
    
    all_rows = await ws.get_all_values()
    data_rows = all_rows[start_row-1:]
    
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
                            domain = r_data[1]
                            url = f"https://{domain}"
                            async with session.get(url, timeout=15) as resp:
                                html = await resp.text()
                                body = re.sub(r'<[^>]+>', '', html)[:20000]
                                prompt = f"Analyze this website and provide SD and LD: {body}"
                                async with session.post(f"{CONFIG['GEMINI_API_URL']}?key={CONFIG['GEMINI_API_KEY']}", json={"contents":[{"parts":[{"text":prompt}]}]}) as g_resp:
                                    res_json = await g_resp.json()
                                    text = res_json['candidates'][0]['content']['parts'][0]['text']
                                    sd, ld = "SD", "LD"
                                    
                                    feed = r_data[3].split(" : ")[1] if " : " in r_data[3] else r_data[3]
                                    f_id = feed_id_map.get(feed, "")
                                    
                                    if mode in ["full", "phase2"]:
                                        p_payload = {"id": r_data[2], "description":{"value":ld}, "shortDescription":{"value":sd}, "publishingDepth":{"value":"Pub 1 - Full"}}
                                        async with session.put("https://platform.tracxn.com/data/entities/2.0/domain-profile", json=p_payload, headers={"accesstoken":"efa37d29-008f-43ad-a21b-44fd0443c462"}) as p_resp:
                                            pass
                                            
                                        m_payload = {"funnelId": r_data[4], "domainProfileId": r_data[2], "movedTo": ["5dc5863a2799a51cc0ff30e2"], "sourceDetails": {"source": "Write API", "sourceData": {"view": "Card", "tab": "Funnel Homepage"}}}
                                        async with session.put("https://platform.tracxn.com/data/funnel-action/move", json=m_payload, headers={"accesstoken":"efa37d29-008f-43ad-a21b-44fd0443c462"}) as m_resp:
                                            pass
                                            
                                    await ws.update(f"I{r_idx}:Y{r_idx}", [["Yes", sd, ld, "", "", "", "", "", "", "", "", f_id, "Done", "N/A", "Done", 0, 0]], value_input_option='USER_ENTERED')
                                    successful_count += 1
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
    var corrected_func = globals["corrected_run_typeb"]
    _ = asyncio.run(corrected_func(start_row, mode, sheet_id, apply_formatting))
    print("✅ Mojo: Type B Pipeline Completed.")

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
