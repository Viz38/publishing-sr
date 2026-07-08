import base64
import json
import os
import asyncio
import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv('/Users/vishnu/Documents/Tracxn/SR/Publishing-Caching/.env')

async def main():
    b64 = os.environ.get('TYPEA_CREDENTIALS_B64')
    creds_dict = json.loads(base64.b64decode(b64).decode('utf-8'))
    
    def get_creds():
        return ServiceAccountCredentials.from_json_keyfile_dict(
            creds_dict,
            ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        )
    
    manager = gspread_asyncio.AsyncioGspreadClientManager(get_creds)
    gc = await manager.authorize()
    
    sheet_id = '1N9GgEXIiR7QwEpzpJCvGbXlZ_kCgN9ev8fj0Ynv98MU'
    sheet = await gc.open_by_key(sheet_id)
    ws = await sheet.worksheet('Prompts')
    
    data = await ws.get_all_values()
    # Let's print out all prompts from index 1 to 10
    for i, row in enumerate(data[1:11]):
        if len(row) > 1:
            print(f"--- PROMPT {i} ---")
            print(repr(row[1]))
            print("------------------")

if __name__ == '__main__':
    asyncio.run(main())
