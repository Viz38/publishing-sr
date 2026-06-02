import asyncio
import aiohttp
import json
import logging
import re
import os
import random
import math
import time
from typing import Optional, Dict, Tuple, Any
from .config import settings
from .models import LLMResult

logger = logging.getLogger("sr_common.utils")

class SystemHealthMonitor:
    """
    Monitors CPU and RAM usage to prevent system saturation.
    Ensures workers only process domains when resources are within safe limits.
    Uses non-blocking cpu_percent(interval=None) to avoid stalling the async event loop.
    """
    def __init__(self, cpu_threshold: float = 90.0, mem_threshold: float = 90.0):
        self.cpu_threshold = cpu_threshold
        self.mem_threshold = mem_threshold
        import psutil
        self._psutil = psutil
        # Prime the CPU counter so subsequent non-blocking reads are meaningful
        self._psutil.cpu_percent(interval=None)

    def is_healthy(self) -> Tuple[bool, str]:
        cpu = self._psutil.cpu_percent(interval=None)
        mem = self._psutil.virtual_memory().percent
        
        if cpu > self.cpu_threshold:
            return False, f"CPU too high ({cpu}%)"
        if mem > self.mem_threshold:
            return False, f"Memory too high ({mem}%)"
        return True, "Healthy"

    async def wait_for_resources(self, logger=None):
        """Pauses execution if system resources are saturated."""
        while True:
            healthy, reason = self.is_healthy()
            if healthy:
                break
            
            if logger:
                logger.warning(f"HEALTH_GATE: Pausing - {reason}")
            await asyncio.sleep(5) # Shorter sleep to prevent AppScript timeouts

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
        
        # Fast path check
        if key in self.caches:
            cache_name, expiry = self.caches[key]
            if cache_name and current_time < (expiry - 300): # 5 mins buffer
                return cache_name
        
        async with self.lock:
            # Recheck inside lock
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

async def call_gemini_api(session: aiohttp.ClientSession, prompt: str, limiter, system_instruction: str = None, cached_content_name: str = None) -> LLMResult:
    import random
    if not prompt or prompt == "noData":
        return LLMResult(text="Error", success=False)
    
    url = f"{settings.GEMINI_API_URL}?key={settings.TYPEA_GEMINI_API_KEY}"
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
                res = await response.json()
                
                if response.status == 429:
                    base_delay = 2 ** (attempt + 1)
                    jitter = random.uniform(0, base_delay)
                    wait_time = base_delay + jitter
                    logging.warning(f"GEMINI 429: Rate limited. Backing off {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_time)
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
            async with session.request(method, url, json=json_data, headers=headers) as response:
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
                
                wait = min(2 * (2**attempt), 60)
                await asyncio.sleep(wait)
                attempt += 1
        except Exception as e:
            logging.error(f"TRACXN EXC: {str(e)} | Attempt: {attempt+1}")
            await asyncio.sleep(2)
            attempt += 1
    return 500, None

def clean_html(html: str) -> str:
    if not html: return ""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    for s in soup(['script', 'style', 'nav', 'footer', 'header']):
        s.decompose()
    return " ".join(soup.get_text(separator=' ').split())

def extract_descriptions(text: str) -> Tuple[str, str]:
    if not text or len(text) < 10:
        return "", ""
    
    # Check for AI refusal or insufficient data signals
    text_lower = text.lower()
    refusal_signals = ["i cannot", "not available", "no information", "insufficient data", "cannot provide", "don't have access", "does not contain"]
    if any(sig in text_lower for sig in refusal_signals):
        return "NO_DATA", "NO_DATA"
    
    # Check if LLM explicitly says it's parked
    if "parked" in text_lower and ("domain" in text_lower or "site" in text_lower or "page" in text_lower):
        return "PARKED_LLM", "PARKED_LLM"

    json_sd = re.search(r'["\']Short Description["\']:\s*["\'](.*?)["\']', text, re.DOTALL)
    json_ld = re.search(r'["\']Long Description["\']:\s*["\'](.*?)["\']', text, re.DOTALL)
    if json_sd and json_ld:
        sd, ld = json_sd.group(1).strip(), json_ld.group(1).strip()
    else:
        sd_m = re.search(r"Short Description:\s*(.*?)(?=\nLong Description:|\n\n|$)", text, re.DOTALL)
        ld_m = re.search(r"Long Description:\s*(.*)", text, re.DOTALL)
        sd, ld = (sd_m.group(1).strip() if sd_m else ""), (ld_m.group(1).strip() if ld_m else "")
        
    return " ".join(sd.split()).rstrip('.'), " ".join(ld.split())

def get_dynamic_max_workers(ram_per_worker_gb: float = 0.2) -> int:
    """
    Calculates the maximum number of concurrent workers based on AVAILABLE system resources.
    Assumes ~200MB per worker (more realistic for browser-heavy tasks).
    User can configure the max via CONFIGURED_MAX_WORKERS, but this function enforces the safe limit.
    It will also respect CONFIGURED_MIN_WORKERS.
    """
    import psutil
    from .config import settings
    
    configured_max = getattr(settings, "CONFIGURED_MAX_WORKERS", 15)
    configured_min = getattr(settings, "CONFIGURED_MIN_WORKERS", 1)
    cores = psutil.cpu_count(logical=False) or 2
    available_mem_gb = psutil.virtual_memory().available / (1024**3)
    
    # 1. CPU-based scaling (2 workers per physical core for browser tasks)
    cpu_limit = cores * 2
    
    # 2. RAM-based scaling (Leave at least 1GB for the OS)
    ram_limit = int(max(0, available_mem_gb - 1.0) / ram_per_worker_gb)
    
    # Safe limit is the lowest of CPU or RAM capacity
    safe_limit = max(1, min(cpu_limit, ram_limit))
    
    # Apply configurations
    calculated_limit = min(configured_max, safe_limit)
    return max(configured_min, calculated_limit)
