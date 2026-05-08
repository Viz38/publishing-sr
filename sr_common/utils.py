import asyncio
import aiohttp
import json
import logging
import re
from typing import Optional, Dict, Tuple, Any
from .config import settings
from .models import LLMResult

logger = logging.getLogger("sr_common.utils")

async def call_gemini_api(session: aiohttp.ClientSession, prompt: str, limiter) -> LLMResult:
    if not prompt or prompt == "noData":
        return LLMResult(text="Error", success=False)
    
    await limiter.throttle()
    url = f"{settings.GEMINI_API_URL}?key={settings.TYPEA_GEMINI_API_KEY}" # Note: We might need to choose the key based on engine
    
    try:
        async with session.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=settings.REQUEST_TIMEOUT) as response:
            res = await response.json()
            if response.status != 200:
                logger.error(f"GEMINI ERR: {response.status} - {res}")
                return LLMResult(text="Error", success=False)
            
            text = res['candidates'][0]['content']['parts'][0]['text']
            usage = res.get("usageMetadata", {})
            return LLMResult(
                text=text,
                prompt_tokens=usage.get("promptTokenCount", 0),
                candidate_tokens=usage.get("candidatesTokenCount", 0),
                success=True
            )
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error(f"GEMINI EXC: {type(e).__name__} - {e}")
        return LLMResult(text="Error", success=False)
    except Exception as e:
        logger.error(f"GEMINI UNKNOWN ERR: {e}")
        return LLMResult(text="Error", success=False)

async def call_tracxn_api(session: aiohttp.ClientSession, url: str, limiter, method: str = "put", json_data: Optional[Dict] = None, headers: Optional[Dict] = None) -> Tuple[int, Optional[Dict]]:
    attempt = 0
    while attempt < settings.MAX_RETRIES:
        try:
            await limiter.throttle()
            async with session.request(method, url, json=json_data, headers=headers) as response:
                status = response.status
                res_data = None
                try:
                    res_data = await response.json()
                except:
                    pass
                
                if status in (200, 201):
                    return status, res_data
                if status in (422, 400, 401, 404):
                    logger.error(f"TRACXN ERR: {status} - {res_data}")
                    return status, None
                
                wait = min(2 * (2**attempt), 60)
                await asyncio.sleep(wait)
                attempt += 1
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"TRACXN EXC: {type(e).__name__} | Attempt: {attempt+1}")
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
    json_sd = re.search(r'["\']Short Description["\']:\s*["\'](.*?)["\']', text, re.DOTALL)
    json_ld = re.search(r'["\']Long Description["\']:\s*["\'](.*?)["\']', text, re.DOTALL)
    if json_sd and json_ld:
        sd, ld = json_sd.group(1).strip(), json_ld.group(1).strip()
    else:
        sd_m = re.search(r"Short Description:\s*(.*?)(?=\nLong Description:|\n\n|$)", text, re.DOTALL)
        ld_m = re.search(r"Long Description:\s*(.*)", text, re.DOTALL)
        sd, ld = (sd_m.group(1).strip() if sd_m else ""), (ld_m.group(1).strip() if ld_m else "")
    return " ".join(sd.split()).rstrip('.'), " ".join(ld.split())
