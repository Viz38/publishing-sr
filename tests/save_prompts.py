import asyncio
import os
import json
from sr_common.clients import GoogleSheetsClient
from sr_common.config import settings

async def main():
    gc = await GoogleSheetsClient.get_manager("TypeA/TypeA.json").authorize()
    prompts_sheet_id = settings.PROMPTS_SHEET_ID
    ws = await (await gc.open_by_key(prompts_sheet_id)).worksheet("Prompts")
    values = await ws.get_all_values()
    
    prompts = [r[1] for r in values[1:10]]
    for i, p in enumerate(prompts):
        with open(f"scratch/prompt_{i}.txt", "w", encoding="utf-8") as f:
            f.write(p)

if __name__ == "__main__":
    os.makedirs("scratch", exist_ok=True)
    asyncio.run(main())
