import asyncio
import aiohttp
import json
import logging
import re
import os
from typing import Optional, Dict, Tuple, Any
from .config import settings
from .models import LLMResult

logger = logging.getLogger("sr_common.utils")

# Load Parked Domain Dictionary
PARKED_KEYWORDS = []
try:
    _parked_file = os.path.join(os.path.dirname(__file__), "parked.txt")
    if os.path.exists(_parked_file):
        with open(_parked_file, "r", encoding="utf-8") as f:
            PARKED_KEYWORDS = [line.strip().lower() for line in f if line.strip() and not line.startswith("#")]
    else:
        logger.warning(f"Parked dictionary missing at {_parked_file}")
except Exception as e:
    logger.error(f"Error loading parked dictionary: {e}")

def is_parked_domain(html: str, text: str) -> Tuple[bool, str]:
    """
    Detects if a domain is parked or for sale using the external dictionary
    and technical signature analysis.
    """
    if not html or not text:
        return False, ""
    
    combined = (html + " " + text).lower()
    
    # Check dictionary
    for kw in PARKED_KEYWORDS:
        if kw in combined:
            return True, kw
            
    # Check for empty titles or technical default patterns
    title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
    if title_match:
        title = title_match.group(1).lower().strip()
        if title in ["under construction", "parked", "coming soon", "welcome to", "plesk", "cpanel"]:
            return True, f"Title: {title}"
            
    return False, ""

async def call_gemini_api(session: aiohttp.ClientSession, prompt: str, limiter) -> LLMResult:
    if not prompt or prompt == "noData":
        return LLMResult(text="Error", success=False)
    
    await limiter.throttle()
    url = f"{settings.GEMINI_API_URL}?key={settings.TYPEA_GEMINI_API_KEY}"
    
    logging.debug(f"GEMINI REQ: Sending prompt (Size: {len(prompt)})")
    try:
        async with session.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=settings.REQUEST_TIMEOUT) as response:
            res = await response.json()
            if response.status != 200:
                logging.error(f"GEMINI ERR {response.status}: {res}")
                return LLMResult(text="Error", success=False)
            
            text = res['candidates'][0]['content']['parts'][0]['text']
            usage = res.get("usageMetadata", {})
            logging.info(f"GEMINI RES: Success (Tokens: {usage.get('totalTokenCount', 0)})")
            return LLMResult(
                text=text,
                prompt_tokens=usage.get("promptTokenCount", 0),
                candidate_tokens=usage.get("candidatesTokenCount", 0),
                success=True
            )
    except Exception as e:
        logging.error(f"GEMINI EXC: {str(e)}")
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
    soup = BeautifulSoup(html, 'html.parser')
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
