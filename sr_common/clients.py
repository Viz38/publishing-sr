import asyncio
import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials
import logging

logger = logging.getLogger("sr_common.clients")

import time

class RateLimiter:
    """Legacy single-process rate limiter (used for Gemini)"""
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

class MultiTierRateLimiter:
    """In-memory sliding-window rate limiter. Replaces the SQLite-backed version
    for ~30x faster throughput. Safe for single-process usage (all engines share
    one Python process). Uses collections.deque per window tier."""
    def __init__(self, db_path: str, limits: dict):
        # db_path kept for API compatibility but ignored
        self.limits = limits  # e.g. {'second': 100, 'minute': 1000, 'hour': 10000, 'day': 100000}
        self.lock = asyncio.Lock()
        self._windows = {'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400}
        self.current_wait_sec = 0  # EXPOSED property for UI feedback
        # One deque per window tier storing timestamps
        from collections import deque
        self._deques = {name: deque() for name in limits if name in self._windows}
            
    async def throttle(self):
        async with self.lock:
            while True:
                now = time.time()
                blocked = False
                max_wait = 0
                for window_name, limit in self.limits.items():
                    if window_name not in self._windows:
                        continue
                    window_sec = self._windows[window_name]
                    dq = self._deques[window_name]
                    # Evict expired entries
                    cutoff = now - window_sec
                    while dq and dq[0] < cutoff:
                        dq.popleft()
                    if len(dq) >= limit:
                        blocked = True
                        wait_for_this_window = dq[0] - cutoff
                        max_wait = max(max_wait, wait_for_this_window)
                if not blocked:
                    self.current_wait_sec = 0
                    # Record this request in all tracked windows
                    for dq in self._deques.values():
                        dq.append(now)
                    return
                # Update exposed property and wait
                self.current_wait_sec = max_wait
                await asyncio.sleep(min(max_wait, 5.0))  # sleep up to 5s to stay responsive to UI checks

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
