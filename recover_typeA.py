import asyncio
import csv
import re
from sr_common.clients import GoogleSheetsClient
from sr_common.config import settings

async def main():
    gc = await GoogleSheetsClient.get_manager("TypeA/TypeA.json").authorize()
    sheet = await gc.open_by_key(settings.TYPEA_SHEET_ID)
    ws = await sheet.worksheet("DB")
    
    # Store latest updates by range
    latest_updates = {}
    
    with open("TypeA/Logs/results_backup.csv", "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or not row[0]:
                continue
            range_val = row[0]
            # Verify range looks like a range (e.g. A7, I7:T7)
            if re.match(r'^[A-Z]+\d+(:[A-Z]+\d+)?$', range_val):
                values = row[1:]
                latest_updates[range_val] = values
    
    # Filter for the target rows that failed
    target_rows = {'7', '10', '11'}
    
    updates = []
    for range_val, values in latest_updates.items():
        row_match = re.search(r'\d+', range_val)
        if row_match and row_match.group() in target_rows:
            # We don't overwrite the original date or simple manual inputs if we don't have to,
            # but since it's the backup, it has exactly what the script wrote.
            updates.append({"range": range_val, "values": [values]})
            print(f"Queueing update for {range_val} -> {values[:2]}...")
            
    if updates:
        print(f"Sending {len(updates)} updates to Google Sheets...")
        await ws.batch_update(updates, value_input_option='USER_ENTERED')
        print("Done!")
    else:
        print("No updates found.")

if __name__ == "__main__":
    asyncio.run(main())
