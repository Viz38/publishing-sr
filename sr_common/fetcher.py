import asyncio
import logging
from typing import Optional, Tuple, Any, Dict
from curl_cffi.requests import AsyncSession
from scrapling import Fetcher, StealthyFetcher
from .stealth import get_human_delay, simulate_human_movement, get_browser_profile

logger = logging.getLogger("fetcher")

# Shared browser semaphore to prevent CPU spikes across engines
BROWSER_SEMAPHORE = asyncio.Semaphore(3)

class StealthFetcher:
    def __init__(self, headers: Optional[Dict[str, str]] = None, timeout: int = 45):
        self.headers = headers or {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-CH-UA': '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
            'Sec-CH-UA-Mobile': '?0',
            'Sec-CH-UA-Platform': '"Windows"',
            'Upgrade-Insecure-Requests': '1'
        }
        self.timeout = timeout

    async def fetch(self, browser, url: str) -> Tuple[Optional[str], str, str]:
        """Orchestrates multi-tier fetching with advanced stealth."""
        logger.info(f"FETCH START: {url}")

        # TIER 0: curl-cffi (TLS/HTTP2 Impersonation)
        try:
            logger.info(f"TIER 0: curl-cffi Fetch for {url}")
            async with AsyncSession(impersonate="chrome120") as s:
                resp = await s.get(url, headers=self.headers, timeout=self.timeout, verify=False)
                if resp.status_code == 200:
                    content = resp.text
                    if self._is_valid(content):
                        logger.info(f"TIER 0 SUCCESS: {url} -> {resp.url}")
                        return content, str(resp.url), "Success"
                    logger.warning(f"TIER 0 FAIL: {url} | Captcha/Low Content")
                else:
                    logger.warning(f"TIER 0 FAIL: {url} | Status: {resp.status_code}")
        except Exception as e:
            logger.warning(f"TIER 0 ERR: {url} | {e}")

        # TIER 1: Scrapling (Basic Request)
        try:
            logger.info(f"TIER 1: Scrapling Fetch for {url}")
            s_fetcher = Fetcher()
            s_resp = await asyncio.to_thread(s_fetcher.get, url)
            if s_resp.status == 200:
                content = s_resp.text
                if self._is_valid(content):
                    logger.info(f"TIER 1 SUCCESS: {url} -> {s_resp.url}")
                    return content, str(s_resp.url), "Success"
        except Exception as e:
            logger.warning(f"TIER 1 ERR: {url} | {e}")

        # TIER 2: Camoufox (Full Browser + Behavior)
        if browser:
            for attempt in range(2):
                context = None
                try:
                    async with BROWSER_SEMAPHORE:
                        logger.info(f"TIER 2: Camoufox Fetch for {url} (attempt {attempt+1})")
                        profile = get_browser_profile("windows")
                        context = await browser.new_context(
                            ignore_https_errors=True,
                            screen_resolution=profile["screen_resolution"],
                            viewport=profile["screen_resolution"],
                            device_scale_factor=profile["device_scale_factor"],
                            user_agent=profile["user_agent"]
                        )
                        page = await context.new_page()
                        
                        # Block media for speed
                        await page.route("**/*", self._block_media)
                        
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                        
                        if response and response.status == 200:
                            await simulate_human_movement(page)
                            await asyncio.sleep(get_human_delay(2.0, 1.0))
                            content = await page.content()
                            if self._is_valid(content, min_len=500):
                                logger.info(f"TIER 2 SUCCESS: {url} -> {page.url}")
                                return content, str(page.url), "Success"
                        break
                except Exception as e:
                    logger.warning(f"TIER 2 ERR: {url} | {e}")
                    if "Proxy" in str(e) and attempt == 0: continue
                finally:
                    if context: await context.close()

        # TIER 3: Scrapling Stealth (Playwright-backed)
        try:
            logger.info(f"TIER 3: Scrapling Stealth for {url}")
            async with BROWSER_SEMAPHORE:
                s_resp = await asyncio.wait_for(
                    StealthyFetcher.async_fetch(url, headless=True, timeout=self.timeout * 1000),
                    timeout=35.0
                )
                if s_resp.status == 200:
                    content = s_resp.text
                    if self._is_valid(content, min_len=300):
                        logger.info(f"TIER 3 SUCCESS: {url} -> {s_resp.url}")
                        return content, str(s_resp.url), "Success"
        except Exception as e:
            logger.warning(f"TIER 3 ERR: {url} | {e}")

        return None, url, "Unable To Scrap"

    def _is_valid(self, content: str, min_len: int = 500) -> bool:
        if not content: return False
        lower_content = content.lower()
        if "sgcaptcha" in lower_content or "challenge-platform" in lower_content:
            return False
        return len(content) > min_len

    async def _block_media(self, route):
        if route.request.resource_type in ["image", "media", "font", "object", "texttrack", "manifest", "other"]:
            await route.abort()
        else:
            await route.continue_()
