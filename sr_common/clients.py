import asyncio
import gspread_asyncio
from oauth2client.service_account import ServiceAccountCredentials
import logging

logger = logging.getLogger("sr_common.clients")

import time
import sqlite3
import os

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
    """Multi-process rate limiter backed by SQLite to maximize global limits across screen sessions."""
    def __init__(self, db_path: str, limits: dict):
        self.db_path = db_path
        self.limits = limits  # e.g. {'second': 100, 'minute': 1000, 'hour': 10000, 'day': 100000}
        self.lock = asyncio.Lock()
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        
        # Initialize DB
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON requests(timestamp)')
            conn.commit()
            
    async def throttle(self):
        async with self.lock:
            while True:
                now = time.time()
                allowed, wait_time = await asyncio.get_event_loop().run_in_executor(None, self._check_and_insert, now)
                if allowed:
                    break
                await asyncio.sleep(wait_time)

    def _check_and_insert(self, now: float) -> tuple[bool, float]:
        windows = {'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400}
        try:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                # Randomly cleanup old records (1% chance) to avoid blocking every call
                import random
                if random.random() < 0.01:
                    conn.execute('DELETE FROM requests WHERE timestamp < ?', (now - 86400,))
                
                max_wait = 0.0
                for window_name, limit in self.limits.items():
                    if window_name not in windows: continue
                    window_sec = windows[window_name]
                    
                    cursor = conn.execute('SELECT COUNT(*) FROM requests WHERE timestamp >= ?', (now - window_sec,))
                    count = cursor.fetchone()[0]
                    
                    if count >= limit:
                        # Find the oldest request in this window to know exactly how long to wait
                        # but for safety just use a static wait and retry
                        max_wait = max(max_wait, 1.0) 
                
                if max_wait > 0:
                    return False, max_wait
                    
                conn.execute('INSERT INTO requests (timestamp) VALUES (?)', (now,))
                conn.commit()
                return True, 0.0
        except sqlite3.OperationalError as e:
            # If database is locked, wait and retry
            return False, 1.0

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
