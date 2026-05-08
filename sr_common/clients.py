import asyncio
import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials
import logging

logger = logging.getLogger("sr_common.clients")

class RateLimiter:
    def __init__(self, rpm: int):
        self.delay = 60.0 / rpm
        self.last_call = 0
        self.lock = asyncio.Lock()

    async def throttle(self):
        async with self.lock:
            now = asyncio.get_event_loop().time()
            wait = self.last_call + self.delay - now
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_call = asyncio.get_event_loop().time()

class GoogleSheetsClient:
    _instance = None
    _manager = None

    @classmethod
    def get_manager(cls, credentials_file: str):
        if cls._manager is None:
            scope = [
                'https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive'
            ]
            creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_file, scope)
            cls._manager = gspread_asyncio.AsyncioGspreadClientManager(lambda: creds)
        return cls._manager
