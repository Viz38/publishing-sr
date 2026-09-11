import asyncio
import logging
from typing import Optional, Tuple, Any, Dict
from curl_cffi.requests import AsyncSession
from scrapling import Fetcher, StealthyFetcher
from .stealth import get_human_delay, simulate_human_movement, get_browser_profile

from .utils import get_dynamic_max_workers

logger = logging.getLogger("fetcher")

# Shared browser semaphore to prevent CPU spikes across engines
BROWSER_SEMAPHORE = asyncio.Semaphore(get_dynamic_max_workers())

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
        urls_to_try = [url]
        if url.startswith("https://"):
            urls_to_try.append(url.replace("https://", "http://", 1))
            
        for attempt_url in urls_to_try:
            try:
                logger.info(f"TIER 0: curl-cffi Fetch for {attempt_url}")
                async with AsyncSession(impersonate="chrome120") as s:
                    resp = await s.get(attempt_url, headers=self.headers, timeout=self.timeout, verify=False)
                    if resp.status_code == 200:
                        content = resp.text
                        if self._is_valid(content):
                            logger.info(f"TIER 0 SUCCESS: {attempt_url} -> {resp.url}")
                            return content, str(resp.url), "Success"
                        logger.warning(f"TIER 0 FAIL: {attempt_url} | Captcha/Low Content")
                    else:
                        logger.warning(f"TIER 0 FAIL: {attempt_url} | Status: {resp.status_code}")
            except Exception as e:
                logger.warning(f"TIER 0 ERR: {attempt_url} | {e}")

        # TIER 2: Camoufox (Full Browser + Behavior)
        if browser:
            for attempt in range(2):
                context = None
                try:
                    async with BROWSER_SEMAPHORE:
                        logger.info(f"TIER 2: Camoufox Fetch for {url} (attempt {attempt+1})")
                        profile = get_browser_profile("windows")
                        screen_res = {"width": profile["screen_resolution"][0], "height": profile["screen_resolution"][1]}
                        
                        async def _run_tier2():
                            ctx = None
                            try:
                                ctx = await browser.new_context(
                                    ignore_https_errors=True,
                                    screen=screen_res,
                                    viewport=screen_res,
                                    device_scale_factor=profile["device_scale_factor"],
                                    user_agent=profile["user_agent"]
                                )
                                page = await ctx.new_page()
                                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                                if response and response.status == 200:
                                    await simulate_human_movement(page)
                                    content = await page.content()
                                    if self._is_valid(content, min_len=500):
                                        from sr_common.utils import clean_html
                                        cleaned = await clean_html(content)
                                        if len(cleaned) > 100:
                                            logger.info(f"TIER 2 SUCCESS: {url} -> {page.url}")
                                            return content, str(page.url), "Success"
                                        else:
                                            logger.warning(f"Tier 2 yielded low text content ({len(cleaned)} chars). Falling back to Tier 3 for {url}")
                                return None, None, "Failed or Low Content"
                            finally:
                                if ctx:
                                    try:
                                        await asyncio.wait_for(ctx.close(), timeout=5.0)
                                    except Exception as ce:
                                        logger.warning(f"TIER 2 ERR: Failed to close context cleanly: {ce}")

                        res_content, res_url, res_status = await asyncio.wait_for(_run_tier2(), timeout=self.timeout + 15.0)
                        if res_content:
                            return res_content, res_url, res_status
                        break
                except asyncio.TimeoutError:
                    logger.error(f"TIER 2 TIMEOUT: Browser pipe hung for {url}")
                except Exception as e:
                    logger.warning(f"TIER 2 ERR: {url} | {e}")
                    if "Proxy" in str(e) and attempt == 0: continue

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
                    if self._is_valid(content, min_len=50):
                        logger.info(f"TIER 3 SUCCESS: {url} -> {s_resp.url}")
                        return content, str(s_resp.url), "Success"
        except Exception as e:
            logger.warning(f"TIER 3 ERR: {url} | {e}")

        return None, url, "Unable To Scrap"

    def _is_valid(self, content: str, min_len: int = 500) -> bool:
        if not content: return False
        lower_content = content.lower()
        if "sgcaptcha" in lower_content or "<title>just a moment...</title>" in lower_content:
            return False
        return len(content) > min_len

    async def _block_media(self, route):
        if route.request.resource_type in ["image", "media", "font", "object", "texttrack", "manifest", "other"]:
            await route.abort()
        else:
            await route.continue_()
