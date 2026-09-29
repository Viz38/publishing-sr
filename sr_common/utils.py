import asyncio
import aiohttp
import json
import logging
import re
import os
import sys
import random
import math
import time
from typing import Optional, Dict, Tuple, Any, Union, List
from .config import settings
from .models import LLMResult

logger = logging.getLogger("sr_common.utils")

_libc = None
if sys.platform.startswith("linux"):
    try:
        import ctypes
        _libc = ctypes.CDLL('libc.so.6')
    except Exception:
        _libc = None

def trim_memory():
    """Runs garbage collection and triggers malloc_trim on Linux to release heap arenas back to OS."""
    import gc
    gc.collect()
    if _libc and hasattr(_libc, 'malloc_trim'):
        try:
            _libc.malloc_trim(0)
        except Exception:
            pass

class SystemHealthMonitor:
    """
    Monitors CPU and RAM usage to prevent system saturation.
    Ensures workers only process domains when resources are within safe limits.
    Uses non-blocking cpu_percent(interval=None) to avoid stalling the async event loop.
    Defaults to 60% on Linux to prevent OS thrashing, and 90%/88% on macOS.
    """
    def __init__(self, cpu_threshold: Optional[float] = None, mem_threshold: Optional[float] = None):
        is_linux = sys.platform.startswith("linux")
        if cpu_threshold is None:
            cpu_threshold = 60.0 if is_linux else 90.0
        if mem_threshold is None:
            mem_threshold = 60.0 if is_linux else 88.0
        self.cpu_threshold = cpu_threshold
        self.mem_threshold = mem_threshold
        import psutil
        self._psutil = psutil
        # Prime the CPU counter so subsequent non-blocking reads are meaningful
        self._psutil.cpu_percent(interval=None)

    def is_healthy(self) -> Tuple[bool, str]:
        cpu = self._psutil.cpu_percent(interval=None)
        if cpu > self.cpu_threshold:
            return False, f"CPU too high ({cpu}%)"

        mem = self._psutil.virtual_memory().percent
        if mem > self.mem_threshold:
            trim_memory()
            mem_after = self._psutil.virtual_memory().percent
            if mem_after > self.mem_threshold:
                return False, f"Memory too high ({mem_after}%)"
        return True, "Healthy"

    async def wait_for_resources(self, logger=None, timeout=None, fast_fail_ram=False):
        """Pauses execution if system resources are saturated. Raises TimeoutError if timeout is reached."""
        start_time = time.time()
        import random
        while True:
            healthy, reason = self.is_healthy()
            if healthy:
                break
                
            if "Memory" in reason:
                trim_memory()
                if fast_fail_ram and logger:
                    logger.warning(f"HEALTH_GATE: RAM saturation observed ({reason}). Reclaiming memory and applying backoff.")
            
            if timeout is not None and (time.time() - start_time) > timeout:
                if logger:
                    logger.error(f"HEALTH_GATE: Timeout exceeded waiting for resources ({reason})")
                raise TimeoutError(f"Resource saturation timeout: {reason}")
            elif timeout is None and (time.time() - start_time) > 120:
                # If no timeout specified, don't hang forever (max 2 mins)
                if logger:
                    logger.warning(f"HEALTH_GATE: Max wait reached, proceeding despite {reason}")
                break
            
            if logger:
                logger.warning(f"HEALTH_GATE: Pausing - {reason}")
                
            if "CPU" in reason:
                # Lightweight micro-pause for transient CPU spikes to protect speed (4K-7K domains/hr)
                await asyncio.sleep(random.uniform(0.5, 1.2))
            else:
                # RAM backpressure with jitter
                await asyncio.sleep(random.uniform(3.0, 6.0))


# Load Parked Domain Dictionary
PARKED_KEYWORDS_STRICT = []
PARKED_KEYWORDS_WEAK = []

try:
    _parked_file = os.path.join(os.path.dirname(__file__), "parked.txt")
    if os.path.exists(_parked_file):
        # High-risk generic words that cause false positives
        BLACKlisted_WEAK = ["registrar", "available", "hosting", "server", "offline", "works", "hello world", "test page", "lorem ipsum", "related searches"]
        
        with open(_parked_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().lower()
                if not line or line.startswith("#"): continue
                
                # Phrases with 3+ words or specific marketplace markers are usually strict
                if len(line.split()) >= 3 or any(m in line for m in ["dan.com", "sedo.com", "afternic", "hugedomains", "domainmarket", "parkingcrew", "bodis"]):
                    PARKED_KEYWORDS_STRICT.append(line)
                elif line not in BLACKlisted_WEAK:
                    PARKED_KEYWORDS_WEAK.append(line)
    else:
        logger.warning(f"Parked dictionary missing at {_parked_file}")
except Exception as e:
    logger.error(f"Error loading parked dictionary: {e}")

def is_parked_domain(html: str, text: str) -> Tuple[bool, str]:
    """
    Detects if a domain is parked or for sale using tiered heuristics.
    """
    if not html: return False, ""
    
    html_lower = html.lower()
    text_lower = text.lower() if text else ""
    
    # --- 1. TECHNICAL SIGNATURES (Highest Confidence) ---
    # Specific Parking Meta Refresh
    if re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=[^>]+(dan\.com|sedo\.com|afternic\.com|hugedomains|bodis|parkingcrew|above\.com|parking\.com)', html_lower):
        return True, "Technical: Meta-Refresh Redirect"
        
    # Specific Parking Script Signatures
    script_markers = ["parkingcrew.net", "sedoparking.com", "bodis.com", "parking.com", "parklogic.com", "afternic.com/for-sale", "domainnameapi.com"]
    if any(s in html_lower for s in script_markers):
        return True, "Technical: Parking Script Signature"

    # Title Patterns
    title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
    if title_match:
        title = title_match.group(1).lower().strip()
        strict_titles = ["under construction", "parked", "coming soon", "welcome to plesk", "welcome to cpanel", "index of /", "default page", "account suspended", "domain is for sale"]
        if any(st == title for st in strict_titles):
            return True, f"Technical Title: {title}"
        if title == "domain for sale" or title == "this domain is for sale":
            return True, "Technical Title: For Sale"

    # --- 2. STRICT KEYWORDS (Word Boundaries) ---
    combined = (html_lower + " " + text_lower)
    for kw in PARKED_KEYWORDS_STRICT:
        # Use regex for word boundaries if it's not a URL-like string
        if "." in kw and not " " in kw:
            if kw in combined: return True, f"Strict Match (URL): {kw}"
        else:
            pattern = rf"\b{re.escape(kw)}\b"
            if re.search(pattern, combined):
                return True, f"Strict Match: {kw}"

    # --- 3. WEAK KEYWORDS (Heuristic Context) ---
    content_length = len(text_lower)
    is_extremely_sparse = content_length < 400
    
    for kw in PARKED_KEYWORDS_WEAK:
        pattern = rf"\b{re.escape(kw)}\b"
        if re.search(pattern, text_lower):
            # Trigger if it's in the title (stronger signal)
            if title_match and kw in title_match.group(1).lower():
                return True, f"Weak Match (Title): {kw}"
            # Trigger if page is extremely sparse (typical of parked pages)
            if is_extremely_sparse:
                return True, f"Weak Match (Sparse Content): {kw}"
            
    return False, ""

class GeminiCacheManager:
    def __init__(self, api_key: str, max_size: int = 50):
        self.api_key = api_key
        self.url = f"{settings.GEMINI_CACHE_URL}?key={api_key}"
        # cache_name -> (key, expiry)
        self.caches: Dict[str, Tuple[str, float]] = {}
        self.lock = asyncio.Lock()
        self.max_size = max_size
        self._synced_remote = False
        self._last_sync_time = 0.0

    async def sync_remote_caches(self, session: aiohttp.ClientSession):
        """Fetches all active caches from Gemini and syncs self.caches"""
        from datetime import datetime
        now = time.time()
        
        # Cooldown check: don't query Google API more than once every 10 seconds
        if now - self._last_sync_time < 10:
            return
            
        try:
            async with session.get(self.url, timeout=15) as response:
                if response.status == 200:
                    data = await response.json()
                    remote_list = data.get("cachedContents", [])
                    
                    # Clear in-memory caches to ensure we don't keep stale local entries
                    # that no longer exist on Google Cloud.
                    self.caches.clear()
                    
                    for item in remote_list:
                        display_name = item.get("displayName")
                        name = item.get("name")
                        expire_time_str = item.get("expireTime")
                        if display_name and name and expire_time_str:
                            try:
                                dt = datetime.fromisoformat(expire_time_str.replace('Z', '+00:00'))
                                expiry = dt.timestamp()
                                if expiry > now + 300: # 5 min safety buffer
                                    key = None
                                    if display_name == "SR_SD_LD_Prompt0":
                                        key = "prompt_0"
                                    elif display_name == "SR_SF_Prompt":
                                        key = "prompt_8"
                                    if key:
                                        self.caches[key] = (name, expiry)
                                        logger.info(f"CACHE SYNCED FROM GEMINI: {key} -> {name} (expires in {expiry - now:.0f}s)")
                            except Exception as parse_err:
                                logger.warning(f"Error parsing cache expireTime {expire_time_str}: {parse_err}")
                    self._synced_remote = True
                    self._last_sync_time = now
                else:
                    logger.warning(f"Failed to sync remote caches: {response.status}")
        except Exception as e:
            logger.warning(f"Exception during remote cache sync: {e}")

    async def _evict_oldest(self, session: aiohttp.ClientSession):
        if not self.caches:
            return
            
        # Find the oldest based on expiry
        oldest_key = min(self.caches.keys(), key=lambda k: self.caches[k][1])
        cache_name, _ = self.caches[oldest_key]
        del self.caches[oldest_key]
        
        # Delete from Gemini API
        if cache_name:
            delete_url = f"https://generativelanguage.googleapis.com/v1beta/{cache_name}?key={self.api_key}"
            try:
                async with session.delete(delete_url, timeout=10) as response:
                    if response.status != 200:
                        logging.warning(f"Failed to explicitly delete cache {cache_name}: {response.status}")
            except Exception as e:
                logging.warning(f"Exception while deleting cache {cache_name}: {e}")

    async def get_or_create(self, session: aiohttp.ClientSession, key: str, system_instruction: str, ttl: str = "3600s") -> Optional[str]:
        current_time = time.time()
        
        # 1. Fast path check: if we have it in memory and it's valid, return it immediately
        if key in self.caches:
            cache_name, expiry = self.caches[key]
            if cache_name and current_time < (expiry - 300): # 5 mins buffer
                return cache_name
        
        async with self.lock:
            # Recheck local cache in case a concurrent request already updated it
            if key in self.caches:
                cache_name, expiry = self.caches[key]
                if cache_name and current_time < (expiry - 300):
                    return cache_name
            
            # Sync from Gemini to see if another process/desktop has refreshed/created it
            await self.sync_remote_caches(session)
            
            # Check again after sync
            if key in self.caches:
                cache_name, expiry = self.caches[key]
                if cache_name and current_time < (expiry - 300):
                    return cache_name
                # If it's expired, we just overwrite it below
                
            model_name = settings.GEMINI_API_URL.split("/v1beta/")[1].split(":")[0]
            payload = {
                "model": model_name,
                "systemInstruction": {"parts": [{"text": system_instruction}]},
                "ttl": ttl
            }
            
            # Map prompt_0 to display name "SR_SD_LD_Prompt0" and prompt_8 to "SR_SF_Prompt"
            if key == "prompt_0":
                payload["displayName"] = "SR_SD_LD_Prompt0"
            elif key == "prompt_8" or key.startswith("prompt_8_"):
                payload["displayName"] = "SR_SF_Prompt"
            
            # LRU Eviction if at max capacity
            if len(self.caches) >= self.max_size and key not in self.caches:
                await self._evict_oldest(session)
            
            ttl_seconds = int(ttl.replace("s", ""))
            
            try:
                async with session.post(self.url, json=payload, timeout=30) as response:
                    if response.status == 200:
                        data = await response.json()
                        cache_name = data["name"]
                        self.caches[key] = (cache_name, current_time + ttl_seconds)
                        logging.info(f"CACHE CREATED: {key} -> {cache_name} (Active: {len(self.caches)}/{self.max_size})")
                        return cache_name
                    else:
                        text = await response.text()
                        if response.status == 400 and "token" in text.lower():
                            logging.warning(f"CACHE SKIPPED for {key} (Min tokens not met). Falling back to non-cached request.")
                        else:
                            logging.warning(f"CACHE CREATE SKIPPED for {key}: {response.status} {text}. Falling back.")
                        # Don't cache failures permanently, but could store a negative cache if needed.
                        # We'll just return None to let caller fall back.
                        return None
            except Exception as e:
                logging.warning(f"CACHE CREATE EXCEPTION for {key}: {e}. Falling back to non-cached request.")
                return None

    async def invalidate(self, key: str):
        async with self.lock:
            if key in self.caches:
                cache_name, _ = self.caches.pop(key)
                logging.info(f"CACHE INVALIDATED: {key} ({cache_name})")

class TrackingCacheManager(GeminiCacheManager):
    """
    Extends GeminiCacheManager with hit/creation metrics and debounced disk persistence.
    Avoids synchronous disk writes on every domain hit.
    """
    def __init__(self, api_key: str, max_size: int = 50, stats_filepath: str = None, debounce_seconds: float = 30.0):
        super().__init__(api_key, max_size)
        self.stats_filepath = stats_filepath or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Logs", "cache_stats.json"
        )
        self.debounce_seconds = debounce_seconds
        self._last_save_time = 0.0
        self.stats = {
            "created_count": 0,
            "used_count": 0,
            "caches": {}  # key -> {cache_name, created_at, expiry, hits}
        }
        self.stats_lock = asyncio.Lock()
        
        # Write initial stats JSON if missing
        try:
            os.makedirs(os.path.dirname(self.stats_filepath), exist_ok=True)
            if not os.path.exists(self.stats_filepath):
                with open(self.stats_filepath, "w", encoding="utf-8") as f:
                    json.dump({
                        "summary": {"total_created": 0, "total_used": 0},
                        "caches": []
                    }, f, indent=4)
        except Exception:
            pass

    async def save_stats(self, force: bool = False):
        async with self.stats_lock:
            now = time.time()
            if not force and (now - self._last_save_time < self.debounce_seconds):
                return
            caches_list = []
            for key, info in self.stats.get("caches", {}).items():
                expiry = info.get("expiry", 0)
                is_expired = now >= expiry
                caches_list.append({
                    "key": key,
                    "cache_id": info.get("cache_name"),
                    "created_at": info.get("created_at"),
                    "used_count": info.get("hits", 0),
                    "expiry_status": "expired" if is_expired else "live",
                    "expiry_raw": expiry
                })
                
            data_to_save = {
                "summary": {
                    "total_created": self.stats.get("created_count", 0),
                    "total_used": self.stats.get("used_count", 0)
                },
                "caches": caches_list
            }
            try:
                temp_path = self.stats_filepath + ".tmp"
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data_to_save, f, indent=4)
                os.replace(temp_path, self.stats_filepath)
                self._last_save_time = now
            except Exception as e:
                logger.error(f"Error saving cache stats: {e}")

    async def get_or_create(self, session: aiohttp.ClientSession, key: str, system_instruction: str, ttl: str = "3600s") -> Optional[str]:
        if not self._synced_remote:
            async with self.lock:
                if not self._synced_remote:
                    await self.sync_remote_caches(session)

        current_time = time.time()
        already_existed = False
        if key in self.caches:
            cache_name, expiry = self.caches[key]
            if cache_name and current_time < (expiry - 300):
                already_existed = True

        cache_name = await super().get_or_create(session, key, system_instruction, ttl)
        
        if cache_name:
            is_new_creation = False
            async with self.stats_lock:
                now = time.time()
                from datetime import datetime
                ttl_seconds = int(ttl.replace("s", ""))
                caches_stats = self.stats.setdefault("caches", {})
                info = caches_stats.get(key)
                is_new_creation = (not already_existed) and (info is None or info.get("cache_name") != cache_name)
                
                if is_new_creation:
                    self.stats["created_count"] = self.stats.get("created_count", 0) + 1
                    caches_stats[key] = {
                        "cache_name": cache_name,
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "expiry": now + ttl_seconds,
                        "hits": 0
                    }
                else:
                    self.stats["used_count"] = self.stats.get("used_count", 0) + 1
                    if key not in caches_stats:
                        caches_stats[key] = {
                            "cache_name": cache_name,
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "expiry": now + ttl_seconds,
                            "hits": 0
                        }
                    caches_stats[key]["hits"] += 1
            
            # Save stats immediately if a new cache was created, otherwise debounce
            await self.save_stats(force=is_new_creation)
            
        return cache_name

    async def invalidate(self, key: str):
        await super().invalidate(key)
        async with self.stats_lock:
            caches_stats = self.stats.setdefault("caches", {})
            if key in caches_stats:
                caches_stats[key]["expiry"] = 0
        await self.save_stats(force=True)

def get_optimized_tcp_connector(
    limit: int = 100,
    limit_per_host: int = 30,
    ttl_dns_cache: int = 300,
    keepalive_timeout: int = 60
) -> aiohttp.TCPConnector:
    """Returns a high-throughput, keepalive-optimized TCPConnector with DNS caching."""
    return aiohttp.TCPConnector(
        limit=limit,
        limit_per_host=limit_per_host,
        ttl_dns_cache=ttl_dns_cache,
        enable_cleanup_closed=True,
        keepalive_timeout=keepalive_timeout
    )

async def confirm_parked_via_llm(session: aiohttp.ClientSession, text: str, limiter, api_key: str = None) -> bool:
    """
    Tier 2 check: Ask Gemini to confirm if the page really looks like a parked domain.
    """
    snippet = text[:2000]
    prompt = PARKED_LLM_PROMPT + snippet
    result = await call_gemini_api(session, prompt, limiter, api_key=api_key)
    if result.success and result.text:
        return "yes" in result.text.lower()
    return False

async def call_gemini_api(session: aiohttp.ClientSession, prompt: str, limiter, api_key: str = None, system_instruction: str = None, cached_content_name: str = None, cache_manager=None, cache_key: str = None) -> LLMResult:
    import random
    if not prompt or prompt == "noData":
        return LLMResult(text="Error", success=False)
    
    target_key = api_key or settings.TYPEA_GEMINI_API_KEY
    url = f"{settings.GEMINI_API_URL}?key={target_key}"
    max_retries = 3
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if cached_content_name:
        payload["cachedContent"] = cached_content_name
    elif system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    
    for attempt in range(max_retries):
        await limiter.throttle()
        logging.debug(f"GEMINI REQ: Sending prompt (Size: {len(prompt)}) | Attempt: {attempt + 1}")
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with session.post(url, json=payload, timeout=timeout) as response:
                try:
                    res = await response.json()
                    err_text = str(res)
                except Exception:
                    res = await response.text()
                    err_text = res
                
                if response.status == 429:
                    base_delay = 2 ** (attempt + 1)
                    jitter = random.uniform(0, base_delay)
                    wait_time = base_delay + jitter
                    logging.warning(f"GEMINI 429: Rate limited. Backing off {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_time)
                    continue
                
                if response.status in (403, 404) and "CachedContent not found" in err_text:
                    if cache_manager and cache_key and system_instruction:
                        logging.warning(f"GEMINI 403/404: CachedContent not found. Invalidating and recreating cache for {cache_key}")
                        await cache_manager.invalidate(cache_key)
                        new_cache_name = await cache_manager.get_or_create(session, cache_key, system_instruction)
                        if new_cache_name:
                            payload["cachedContent"] = new_cache_name
                        elif "cachedContent" in payload:
                            del payload["cachedContent"]
                        continue
                
                if response.status != 200:
                    logging.error(f"GEMINI ERR {response.status}: {res} | Attempt {attempt + 1}/{max_retries}")
                    
                    # Fallback for expired cache or token errors from Gemini
                    err_str = str(res).lower()
                    if response.status in (400, 404) and ("cachedcontent" in err_str or "token" in err_str or "cache" in err_str):
                        logging.warning("Cache/Token error detected during generation. Retrying without cache.")
                        if "cachedContent" in payload:
                            del payload["cachedContent"]
                        if system_instruction:
                            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
                        await asyncio.sleep(1)
                        continue
                        
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** (attempt + 1))
                        continue
                    return LLMResult(text="Error", success=False)
                
                parts = res.get('candidates', [{}])[0].get('content', {}).get('parts', [])
                text_parts = []
                thinking_parts = []
                for p in parts:
                    if p.get("thought") == True:
                        thinking_parts.append(p.get("text", ""))
                    else:
                        text_parts.append(p.get("text", ""))
                
                text = "".join(text_parts)
                thinking_text = "".join(thinking_parts)
                
                usage = res.get("usageMetadata", {})
                think_toks = usage.get("thoughtsTokenCount", usage.get("thoughts_token_count", usage.get("reasoningTokenCount", 0)))
                cached_toks = usage.get("cachedContentTokenCount", usage.get("cached_content_token_count", 0))
                logging.info(f"GEMINI RES: Success (Tokens: {usage.get('totalTokenCount', 0)} | Think: {think_toks} | Cache: {cached_toks})")
                return LLMResult(
                    text=text,
                    thinking_text=thinking_text,
                    prompt_tokens=usage.get("promptTokenCount", 0),
                    candidate_tokens=usage.get("candidatesTokenCount", 0),
                    thinking_tokens=think_toks,
                    cached_tokens=cached_toks,
                    success=True
                )
        except Exception as e:
            logging.error(f"GEMINI EXC: {str(e)} | Attempt: {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            return LLMResult(text="Error", success=False)
    
    logging.error("GEMINI FAIL: Exhausted all retries after 429 rate limiting")
    return LLMResult(text="Error", success=False)

async def call_tracxn_api(session: aiohttp.ClientSession, url: str, limiter, method: str = "put", json_data: Optional[Dict] = None, headers: Optional[Dict] = None) -> Tuple[int, Optional[Dict]]:
    attempt = 0
    while attempt < settings.MAX_RETRIES:
        try:
            await limiter.throttle()
            logging.info(f"TRACXN REQ: {method.upper()} {url} | Payload: {json.dumps(json_data)}")
            async with session.request(method, url, json=json_data, headers=headers, timeout=aiohttp.ClientTimeout(total=45)) as response:
                status = response.status
                res_data = None
                try:
                    res_data = await response.json()
                except:
                    pass
                
                logging.info(f"TRACXN RES: {status} | Data: {json.dumps(res_data)}")
                
                if status in (200, 201):
                    return status, res_data
                if status in (422, 400, 401, 404):
                    return status, res_data
                
                # Confirmed rate limiting from API response
                is_rate_limit = (status == 429) or (
                    status == 403 and res_data and any(
                        kw in str(res_data).lower() for kw in ["rate limit", "too many requests", "throttle"]
                    )
                )
                if is_rate_limit:
                    retry_after = response.headers.get("Retry-After")
                    sleep_duration = 60.0
                    if retry_after:
                        try:
                            sleep_duration = min(float(retry_after), 60.0)
                        except (ValueError, TypeError):
                            sleep_duration = 60.0
                    sleep_duration = max(0.1, min(sleep_duration, 60.0))
                    logging.warning(
                        f"TRACXN RATE LIMIT CONFIRMED (Status {status}): Putting key to sleep for {sleep_duration:.1f}s"
                    )
                    if hasattr(limiter, 'pause'):
                        limiter.pause(sleep_duration, message=f"Tracxn API Rate Limit (HTTP {status}) hit. Paused for cooldown.")
                    await asyncio.sleep(sleep_duration)
                    attempt += 1
                    continue

                wait = min(2 * (2**attempt), 60)
                await asyncio.sleep(wait)
                attempt += 1
        except Exception as e:
            logging.error(f"TRACXN EXC: {str(e)} | Attempt: {attempt+1}")
            await asyncio.sleep(2)
            attempt += 1
    return 500, None

async def clean_html(html: str) -> str:
    if not html: return ""
    
    def _clean(h):
        from bs4 import BeautifulSoup
        import json
        soup = BeautifulSoup(h, 'lxml')
        
        # Extract JSON-LD and application/json state
        json_texts = []
        for s in soup('script'):
            t = s.get('type', '').lower()
            if t in ['application/json', 'application/ld+json'] and s.string:
                json_texts.append(s.string)
                
        for s in soup(['script', 'style', 'nav', 'footer', 'header']):
            s.decompose()
            
        base_text = " ".join(soup.get_text(separator=' ').split())
        return base_text + " " + " ".join(json_texts)
        
    return await asyncio.to_thread(_clean, html)

def extract_descriptions(text: str) -> Tuple[str, str]:
    if not text or len(text) < 10:
        return "", ""
    
    # Check for AI refusal or insufficient data signals
    text_lower = text.lower()
    refusal_signals = ["i cannot", "not available", "no information", "insufficient data", "cannot provide", "don't have access", "does not contain","content insufficient", "content insufficient for generating sd or ld"]
    if any(sig in text_lower for sig in refusal_signals):
        return "NO_DATA", "NO_DATA"
    
    # Check if LLM explicitly says it's parked
    if "parked" in text_lower and ("domain" in text_lower or "site" in text_lower or "page" in text_lower):
        return "PARKED_LLM", "PARKED_LLM"

    json_sd = re.search(r'["\']\*?\*?(?:Short Description|SD)\*?\*?["\']:\s*["\'](.*?)["\']', text, re.IGNORECASE | re.DOTALL)
    json_ld = re.search(r'["\']\*?\*?(?:Long Description|LD)\*?\*?["\']:\s*["\'](.*?)["\']', text, re.IGNORECASE | re.DOTALL)
    if json_sd and json_ld:
        sd, ld = json_sd.group(1).strip(), json_ld.group(1).strip()
    else:
        sd_m = re.search(r'\*?\*?(?:Short Description|SD)\*?\*?:\s*(.*?)(?=\n\*?\*?(?:Long Description|LD)\*?\*?:|\n\n|$)', text, re.IGNORECASE | re.DOTALL)
        ld_m = re.search(r'\*?\*?(?:Long Description|LD)\*?\*?:\s*(.*)', text, re.IGNORECASE | re.DOTALL)
        sd, ld = (sd_m.group(1).strip() if sd_m else ""), (ld_m.group(1).strip() if ld_m else "")
        
    return " ".join(sd.split()).rstrip('.'), " ".join(ld.split())

def col_to_index(col: Union[str, int]) -> int:
    """Converts Excel-style column string ('A', 'B', 'S', 'X', 'AB', 'AC') to 0-based index."""
    if isinstance(col, int):
        return col
    idx = 0
    for char in str(col).strip().upper():
        if 'A' <= char <= 'Z':
            idx = idx * 26 + (ord(char) - ord('A') + 1)
    return max(0, idx - 1)

def is_tc_scraper_mode(val: Any) -> bool:
    """
    Checks if a cell value specifies TC (Tech Crawler) mode.
    Returns True if value matches 'TC', 'Tech', 'Tech Crawler' (case-insensitive).
    Returns False for 'BU', empty/None, or any other value (defaults to BU).
    """
    if not val:
        return False
    v = str(val).strip().lower()
    return v in ("tc", "tech", "tech crawler") or v.startswith("tc") or v.startswith("tech")

def get_dynamic_max_workers(ram_per_worker_gb: float = 0.45) -> int:
    """
    Calculates the maximum number of concurrent workers based on AVAILABLE system resources.
    Assumes ~450MB per worker (handling full HTML, subpages, and Gemini responses).
    Leaves at least 2GB of headroom for the OS and desktop UI.
    On Linux: strictly capped at 6 workers max and cores * 2 to prevent CPU saturation.
    On macOS / other OS: scales dynamically up to configured_max and cores * 4.
    """
    import psutil
    from .config import settings
    
    is_linux = sys.platform.startswith("linux")
    configured_max = getattr(settings, "CONFIGURED_MAX_WORKERS", 12)
    if is_linux:
        configured_max = min(configured_max, 6)
    configured_min = getattr(settings, "CONFIGURED_MIN_WORKERS", 1)
    cores = psutil.cpu_count(logical=False) or 2
    available_mem_gb = psutil.virtual_memory().available / (1024**3)
    
    # 1. CPU-based scaling (2 per core on Linux to preserve UI, 4 on macOS)
    cpu_limit = cores * 2 if is_linux else cores * 4
    
    # 2. RAM-based scaling (Leave at least 2GB for the OS and desktop)
    ram_limit = int(max(0, available_mem_gb - 2.0) / ram_per_worker_gb)
    
    # Bound by configured_max, cpu_limit, and ram_limit
    safe_limit = max(1, min(configured_max, min(cpu_limit, ram_limit)))
    
    return max(configured_min, safe_limit)


async def reconcile_sheet_gaps(
    ws,
    start_row: int,
    data_rows: list,
    csv_records: Any = None,
    h_map: Optional[dict] = None,
    stat_col: Optional[str] = None
) -> Tuple[List[int], List[int]]:
    """
    Audits the Google Sheet for missing/blank rows after processing.
    Attempts to restore missing rows from CSV backup records.
    Returns:
        reconciled: List[int] of row indices that were successfully restored/written to the sheet.
        still_missing: List[int] of row indices that remain blank in both sheet and CSV.
    """
    h_map = h_map or {}
    stat_col = stat_col or h_map.get("r1", "H")
    if not data_rows:
        return [], []
        
    row_indices = [idx for idx, _ in data_rows]
    min_row = min(row_indices)
    max_row = max(row_indices)
    
    # Read existing status values from sheet
    range_str = f"{stat_col}{min_row}:{stat_col}{max_row}"
    try:
        values = await ws.get_values(range_str)
    except TypeError:
        values = await ws.get_values()
    except Exception as e:
        logger.error(f"Error reading status column {range_str} during gap audit: {e}")
        values = []
        
    gap_rows = []
    for idx, row_data in data_rows:
        offset = idx - min_row
        val = ""
        if values and 0 <= offset < len(values):
            row_val = values[offset]
            if row_val and len(row_val) > 0:
                val = str(row_val[0]).strip()
        if not val:
            gap_rows.append(idx)
            
    if not gap_rows:
        return [], []
        
    records_map = {}
    if isinstance(csv_records, dict):
        records_map = csv_records
    else:
        csv_path = csv_records if isinstance(csv_records, str) else os.path.join(os.path.dirname(os.path.dirname(__file__)), "Logs", "results_backup.csv")
        if os.path.exists(csv_path):
            import csv
            try:
                with open(csv_path, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    for r in reader:
                        if not r or r[0] == "Range":
                            continue
                        range_name = r[0]
                        m = re.search(r'\d+', range_name)
                        if m:
                            row_num = int(m.group())
                            records_map.setdefault(row_num, []).append({
                                "range": range_name,
                                "values": [r[1:]]
                            })
            except Exception as e:
                logger.error(f"Error loading backup CSV {csv_path}: {e}")
                
    reconciled = []
    still_missing = []
    batch_payload = []
    
    for idx in gap_rows:
        if idx in records_map:
            batch_payload.extend(records_map[idx])
            reconciled.append(idx)
        else:
            still_missing.append(idx)
            
    if batch_payload:
        try:
            await ws.batch_update(batch_payload)
            logger.info(f"AUDIT: Flushed {len(batch_payload)} reconciled ranges to sheet for rows: {reconciled}")
        except Exception as e:
            logger.error(f"AUDIT: Failed to push reconciled updates to sheet: {e}")
            still_missing.extend(reconciled)
            reconciled = []
            
    return reconciled, still_missing


async def update_manual_curation_date(session: aiohttp.ClientSession, company_id: str, limiter, headers: dict) -> tuple:
    """Updates the manual curation date of a company to today's date"""
    from datetime import datetime
    today = datetime.today()
    payload = {
        "id": company_id,
        "manualCurationDate": {
            "year": today.year,
            "month": today.month,
            "day": today.day
        }
    }
    url = "https://platform.tracxn.com/data/entities/2.0/company"
    return await call_tracxn_api(session, url, limiter, method="put", json_data=payload, headers=headers)
