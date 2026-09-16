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
        self._paused_until = 0.0

    def pause(self, seconds: float = 60.0):
        """Put the key/limiter to sleep for at most 60 seconds on confirmed API rate limit."""
        duration = min(max(float(seconds), 0.1), 60.0)
        now = time.time()
        self._paused_until = max(self._paused_until, now + duration)

    def get_pause_status(self) -> dict:
        now = time.time()
        if now < self._paused_until:
            rem = max(0.0, self._paused_until - now)
            return {
                "paused": True,
                "remaining": round(rem, 1),
                "message": f"Rate Limit hit. Pausing for {round(rem, 1)}s."
            }
        return {"paused": False, "remaining": 0.0, "message": ""}

    async def throttle(self):
        while True:
            now = time.time()
            if now < self._paused_until:
                await asyncio.sleep(min(self._paused_until - now, 2.0))
                continue
            async with self.lock:
                loop_now = asyncio.get_event_loop().time()
                wait = self.last_call + self.delay - loop_now
                if wait > 0:
                    await asyncio.sleep(wait)
                self.last_call = asyncio.get_event_loop().time()
                return

class MultiTierRateLimiter:
    """In-memory sliding-window rate limiter with sub-millisecond precision.
    Supports single-second or multi-tier rate limits, explicit API rate-limit
    key-pausing, and lock-free sleeping to eliminate worker contention."""
    def __init__(self, db_path: str = None, limits: dict = None):
        # db_path kept for backward API compatibility
        from collections import deque
        if limits is None:
            self.limits = {'second': 95}
        else:
            self.limits = dict(limits)
        self.lock = asyncio.Lock()
        self._windows = {'second': 1.0, 'minute': 60.0, 'hour': 3600.0, 'day': 86400.0}
        self._deques = {name: deque() for name in self.limits if name in self._windows}
        self._paused_until = 0.0
        self._last_warn_time = {}

    def pause(self, seconds: float = 60.0, message: str = None):
        """Put the key/limiter to sleep for at most 60 seconds when an API response
        confirms a rate limit (e.g. HTTP 429)."""
        duration = min(max(float(seconds), 0.1), 60.0)
        now = time.time()
        new_pause = now + duration
        if new_pause > self._paused_until:
            self._paused_until = new_pause
            self._pause_message = message or "Tracxn API Rate Limit (HTTP 429) hit. Paused for cooldown."
            logger.warning(
                f"TRACXN RATE LIMIT CONFIRMED (API Response): Putting key to sleep for {duration:.1f}s "
                f"until {time.strftime('%H:%M:%S', time.localtime(self._paused_until))}"
            )

    def get_pause_status(self) -> dict:
        now = time.time()
        if now < self._paused_until:
            rem = max(0.0, self._paused_until - now)
            return {
                "paused": True,
                "remaining": round(rem, 1),
                "message": getattr(self, "_pause_message", "Tracxn API Rate Limit (HTTP 429) hit. Paused for cooldown.")
            }
        return {"paused": False, "remaining": 0.0, "message": ""}

    async def throttle(self):
        while True:
            now = time.time()
            # 1. Check if key is currently paused from confirmed API rate limit response
            if now < self._paused_until:
                sleep_time = min(self._paused_until - now, 2.0)
                await asyncio.sleep(sleep_time)
                continue

            wait_sec = 0.0
            hit_window = None
            hit_limit = 0

            # 2. Inspect and prune windows inside minimal lock scope
            async with self.lock:
                now = time.time()
                if now < self._paused_until:
                    blocked = True
                    wait_sec = min(self._paused_until - now, 2.0)
                else:
                    blocked = False
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
                            hit_window = window_name
                            hit_limit = limit
                            # Calculate time until earliest entry in this window expires
                            time_to_free = (dq[0] + window_sec) - now
                            wait_sec = max(wait_sec, max(0.01, time_to_free + 0.005))

                    if not blocked:
                        # Success: record timestamp in all active windows
                        for dq in self._deques.values():
                            dq.append(now)
                        return

            # 3. Lock is released before sleeping! Other coroutines are never locked out.
            if hit_window:
                last_warn = self._last_warn_time.get(hit_window, 0.0)
                if now - last_warn >= 10.0:  # Debounce logging to at most once per 10s
                    self._last_warn_time[hit_window] = now
                    logger.warning(
                        f"TRACXN LIMITER: Window '{hit_window}' limit ({hit_limit}) reached. "
                        f"Throttling for {wait_sec:.2f}s..."
                    )

            await asyncio.sleep(min(wait_sec, 1.0))

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
