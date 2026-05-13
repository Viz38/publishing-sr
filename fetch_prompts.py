import asyncio
import os
import json
from sr_common.clients import GoogleSheetsClient
from sr_common.config import settings

async def main():
    gc = await GoogleSheetsClient.get_manager("TypeA/TypeA.json").authorize()
    prompts_sheet_id = settings.PROMPTS_SHEET_ID
    print(f"Fetching prompts from {prompts_sheet_id}")
    ws = await (await gc.open_by_key(prompts_sheet_id)).worksheet("Prompts")
    values = await ws.get_all_values()
    
    prompts = [r[1] for r in values[1:10]]
    for i, p in enumerate(prompts):
        print(f"--- Prompt {i} ---")
        print(p)
        print()

if __name__ == "__main__":
    asyncio.run(main())
