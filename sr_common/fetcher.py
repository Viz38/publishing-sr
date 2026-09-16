import asyncio
import logging
from typing import Optional, Tuple, Any, Dict
from curl_cffi.requests import AsyncSession
from scrapling import Fetcher, StealthyFetcher
from camoufox.async_api import AsyncCamoufox
from browserforge.fingerprints import Screen
from .stealth import get_human_delay, simulate_human_movement, get_browser_profile

from .utils import get_dynamic_max_workers

logger = logging.getLogger("fetcher")

def is_permanent_network_error(err: Exception) -> bool:
    """Detects fatal DNS resolution errors or connection refusal to fast-fail without slow browser fallback."""
    msg = str(err).lower()
    # curl (6) = Could not resolve host (DNS failure)
    # curl (7) = Failed to connect (Connection refused/port closed)
    if "(6)" in msg or "could not resolve host" in msg or "name or service not known" in msg:
        return True
    if "(7)" in msg or "failed to connect" in msg or "connection refused" in msg:
        return True
    return False

class _LazySemaphore:
    """Loop-safe lazy semaphore that bounds concurrent browser processes."""
    def __init__(self):
        self._sem = None

    def _get(self):
        if self._sem is None:
            self._sem = asyncio.Semaphore(get_dynamic_max_workers())
        return self._sem

    async def __aenter__(self):
        return await self._get().__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self._get().__aexit__(exc_type, exc_val, exc_tb)

class BrowserManager:
    """
    Thread-safe and async-safe manager for browser lifecycle.
    Provides lazy initialization, health recycling after N navigations,
    and graceful crash recovery without canceling active worker tasks.
    """
    def __init__(self, max_navigations: int = 150, grace_period: float = 25.0):
        self.max_navigations = max_navigations
        self.grace_period = grace_period
        self.nav_count = 0
        self._browser = None
        self._camoufox = None
        self._lock = asyncio.Lock()
        self._pending_close = []

    async def get_browser(self):
        async with self._lock:
            if self._browser is not None:
                mem_high = False
                try:
                    import psutil
                    if psutil.virtual_memory().percent > 88:
                        mem_high = True
                except Exception:
                    pass

                if self.nav_count >= self.max_navigations or mem_high:
                    logger.info(f"BROWSER_MGR: Recycling browser after {self.nav_count} navigations (mem_high={mem_high})")
                    old_browser = self._browser
                    self._browser = None
                    self._camoufox = None
                    self.nav_count = 0

                    if old_browser:
                        if self.grace_period <= 0:
                            try:
                                await old_browser.close()
                            except Exception as e:
                                logger.warning(f"BROWSER_MGR: Error closing old browser: {e}")
                        else:
                            self._pending_close.append(old_browser)
                            async def _delayed_close(b):
                                try:
                                    await asyncio.sleep(self.grace_period)
                                    await b.close()
                                except Exception as e:
                                    logger.warning(f"BROWSER_MGR: Error during graceful close of old browser: {e}")
                                finally:
                                    if b in self._pending_close:
                                        self._pending_close.remove(b)
                            asyncio.create_task(_delayed_close(old_browser))

            if self._browser is None:
                logger.info("BROWSER_MGR: Launching fresh AsyncCamoufox instance...")
                profile = get_browser_profile("windows")
                self._camoufox = AsyncCamoufox(
                    headless=True,
                    humanize=True,
                    block_webrtc=True,
                    os=profile["os"],
                    screen=Screen(max_width=profile["screen_resolution"][0], max_height=profile["screen_resolution"][1]),
                    i_know_what_im_doing=True
                )
                self._browser = await self._camoufox.__aenter__()
                self.nav_count = 0

            self.nav_count += 1
            return self._browser

    async def close(self):
        async with self._lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
                self._camoufox = None
            for b in list(self._pending_close):
                try:
                    await b.close()
                except Exception:
                    pass
            self._pending_close.clear()

# Shared browser semaphore to prevent CPU spikes across engines
BROWSER_SEMAPHORE = _LazySemaphore()

class StealthFetcher:
    def __init__(self, headers: Optional[Dict[str, str]] = None, timeout: int = 12):
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
        """Orchestrates multi-tier fetching with advanced stealth and fast-fail defense."""
        logger.info(f"FETCH START: {url}")

        try:
            return await asyncio.wait_for(self._fetch_internal(browser, url), timeout=35.0)
        except asyncio.TimeoutError:
            logger.warning(f"GLOBAL FETCH TIMEOUT: {url} exceeded 35s total budget")
            return None, url, "Unable To Scrap"

    async def _fetch_internal(self, browser, url: str) -> Tuple[Optional[str], str, str]:
        # TIER 0: curl-cffi (TLS/HTTP2 Impersonation)
        urls_to_try = [url]
        if url.startswith("https://"):
            urls_to_try.append(url.replace("https://", "http://", 1))

        tier0_saw_403 = False
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
                    elif resp.status_code in (403, 401, 503):
                        tier0_saw_403 = True
                        logger.warning(f"TIER 0 FAIL: {attempt_url} | Status: {resp.status_code} (WAF/Challenge)")
                    else:
                        logger.warning(f"TIER 0 FAIL: {attempt_url} | Status: {resp.status_code}")
            except Exception as e:
                logger.warning(f"TIER 0 ERR: {attempt_url} | {e}")
                if is_permanent_network_error(e):
                    logger.info(f"TIER 0 FAST-FAIL: {attempt_url} | Unreachable host ({e})")
                    return None, url, "Unable To Scrap"

        # TIER 2: Camoufox (Full Browser + Behavior)
        if browser:
            b_instance = browser
            if hasattr(browser, "get_browser"):
                try:
                    b_instance = await browser.get_browser()
                except Exception as be:
                    logger.warning(f"TIER 2 ERR: Failed to get browser from manager: {be}")
                    b_instance = None

            if b_instance:
                for attempt in range(1):
                    try:
                        async with BROWSER_SEMAPHORE:
                            logger.info(f"TIER 2: Camoufox Fetch for {url} (attempt {attempt+1})")
                            profile = get_browser_profile("windows")
                            screen_res = {"width": profile["screen_resolution"][0], "height": profile["screen_resolution"][1]}

                            async def _run_tier2():
                                ctx = None
                                try:
                                    ctx = await b_instance.new_context(
                                        ignore_https_errors=True,
                                        screen=screen_res,
                                        viewport=screen_res,
                                        device_scale_factor=profile["device_scale_factor"],
                                        user_agent=profile["user_agent"]
                                    )
                                    page = await ctx.new_page()
                                    browser_timeout_ms = min(self.timeout * 1000, 20000)
                                    response = await page.goto(url, wait_until="domcontentloaded", timeout=browser_timeout_ms)
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
                                            await asyncio.wait_for(ctx.close(), timeout=3.0)
                                        except Exception as ce:
                                            logger.warning(f"TIER 2 ERR: Failed to close context cleanly: {ce}")

                        res_content, res_url, res_status = await asyncio.wait_for(_run_tier2(), timeout=25.0)
                        if res_content:
                            return res_content, res_url, res_status
                        break
                    except asyncio.TimeoutError:
                        logger.error(f"TIER 2 TIMEOUT: Browser navigation exceeded 25s for {url}")
                        break
                    except Exception as e:
                        err_msg = str(e)
                        logger.warning(f"TIER 2 ERR: {url} | {err_msg}")
                        if "NS_ERROR_UNKNOWN_HOST" in err_msg or "ERR_NAME_NOT_RESOLVED" in err_msg:
                            logger.info(f"TIER 2 FAST-FAIL: {url} | DNS unresolvable in browser")
                            return None, url, "Unable To Scrap"
                        if "Proxy" in err_msg and attempt == 0:
                            continue
                        break

        # TIER 3: Scrapling Stealth (Playwright-backed)
        try:
            logger.info(f"TIER 3: Scrapling Stealth for {url}")
            async with BROWSER_SEMAPHORE:
                s_resp = await asyncio.wait_for(
                    StealthyFetcher.async_fetch(url, headless=True, timeout=15000),
                    timeout=18.0
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
