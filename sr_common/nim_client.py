import asyncio
import logging
import os
import time
from typing import Dict, List, Optional, Set, Tuple
import aiohttp

from sr_common.config import settings
from sr_common.models import LLMResult

logger = logging.getLogger("sr_common.nim_client")

class NIMKeyManager:
    """
    Async thread-safe round-robin token manager for NVIDIA NIM API keys.
    Supports pooling up to 20+ keys loaded dynamically from Google Sheets,
    automatic 60s cooldown penalties on HTTP 429 rate limits, and permanent deactivation for 401/403.
    """
    def __init__(
        self,
        keys: Optional[List[str]] = None,
        sheet_id: Optional[str] = None,
        tab_name: str = "SS keys",
        credentials_file: Optional[str] = None
    ):
        raw_keys = [str(k).strip() for k in (keys or []) if str(k).strip()]
        self.keys: List[str] = list(dict.fromkeys(raw_keys))
        self.invalid_keys: Set[str] = set()
        self._penalties: Dict[str, float] = {}
        self._index: int = 0
        self._lock = asyncio.Lock()
        
        self.sheet_id = sheet_id or settings.NVIDIA_KEYS_SHEET_ID
        self.tab_name = tab_name or settings.NVIDIA_KEYS_TAB_NAME
        self.credentials_file = credentials_file

    @property
    def total_keys(self) -> int:
        return len(self.keys)

    @property
    def active_keys_count(self) -> int:
        now = time.time()
        return sum(1 for k in self.keys if self._penalties.get(k, 0.0) <= now)

    async def load_keys_from_sheet(self, credentials_file: Optional[str] = None) -> List[str]:
        """
        Loads NIM API keys from the designated Google Sheet worksheet.
        """
        creds_path = credentials_file or self.credentials_file
        if not creds_path or not os.path.exists(creds_path):
            logger.warning(f"NIMKeyManager: Credentials file not found: {creds_path}")
            return self.keys

        target_sheet_id = self.sheet_id or settings.NVIDIA_KEYS_SHEET_ID
        if not target_sheet_id:
            logger.warning("NIMKeyManager: NVIDIA_KEYS_SHEET_ID not configured.")
            return self.keys

        try:
            from sr_common.clients import GoogleSheetsClient
            manager = GoogleSheetsClient.get_manager(creds_path)
            gc = await manager.authorize()
            sh = await gc.open_by_key(target_sheet_id)
            ws = await sh.worksheet(self.tab_name)
            all_vals = await ws.get_all_values()

            extracted = []
            for r in all_vals:
                if not r:
                    continue
                val = str(r[0]).strip()
                if val and val.startswith("nvapi-"):
                    extracted.append(val)

            async with self._lock:
                for k in extracted:
                    if k not in self.invalid_keys and k not in self.keys:
                        self.keys.append(k)

            logger.info(f"NIMKeyManager: Successfully loaded {len(extracted)} NIM keys from sheet {target_sheet_id} [{self.tab_name}]. Active pool: {len(self.keys)}")
            return self.keys
        except Exception as e:
            logger.error(f"NIMKeyManager: Failed to load keys from sheet: {e}")
            return self.keys

    def mark_rate_limited(self, key: str, penalty_sec: float = 60.0) -> None:
        """Applies a temporary cooldown penalty (default 60s) on HTTP 429 rate limit."""
        if not key or key in self.invalid_keys:
            return
        masked = f"...{key[-6:]}" if len(key) >= 6 else "key"
        self._penalties[key] = time.time() + penalty_sec
        logger.warning(f"NIM token {masked} penalized for {penalty_sec:.1f}s (HTTP 429).")

    def mark_invalid(self, key: str) -> None:
        """Permanently deactivates an invalid or expired token (HTTP 401/403)."""
        if not key:
            return
        masked = f"...{key[-6:]}" if len(key) >= 6 else "key"
        self.invalid_keys.add(key)
        if key in self.keys:
            self.keys.remove(key)
        if key in self._penalties:
            del self._penalties[key]
        logger.warning(f"NIM token {masked} permanently deactivated (HTTP 401/403). Remaining keys: {len(self.keys)}")

    async def acquire_key(self) -> str:
        """
        Leases the next active, non-penalized NIM key in round-robin fashion.
        If all active keys are cooling down, pauses briefly until the nearest penalty expires.
        """
        while True:
            async with self._lock:
                if not self.keys:
                    logger.error("NIMKeyManager: No active NIM keys available.")
                    return ""

                now = time.time()
                # Find available keys
                for _ in range(len(self.keys)):
                    k = self.keys[self._index % len(self.keys)]
                    self._index += 1
                    if self._penalties.get(k, 0.0) <= now:
                        return k

                # If all keys are penalized, find minimum remaining sleep time
                min_wait = min(self._penalties.get(k, now) - now for k in self.keys)
                wait_time = max(0.1, min(min_wait, 15.0))

            logger.info(f"All {len(self.keys)} NIM keys cooling down. Throttling {wait_time:.1f}s...")
            await asyncio.sleep(wait_time)


async def call_nvidia_nim_api(
    session: aiohttp.ClientSession,
    prompt: str,
    key_manager: NIMKeyManager,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
    system_instruction: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    max_retries: int = 4
) -> LLMResult:
    """
    Executes an asynchronous chat completion request against NVIDIA NIM API.
    Handles round-robin key rotation, HTTP 429 cooldowns, 401 invalidation,
    and returns a standardized LLMResult.
    """
    if not prompt or prompt == "noData":
        return LLMResult(text="Error", success=False)

    target_endpoint = endpoint or settings.NVIDIA_API_URL
    target_model = model or settings.NVIDIA_MODEL

    messages = []
    if system_instruction and str(system_instruction).strip():
        messages.append({"role": "system", "content": str(system_instruction).strip()})
    messages.append({"role": "user", "content": str(prompt)})

    payload = {
        "model": target_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False
    }

    timeout = aiohttp.ClientTimeout(total=75.0, sock_connect=10.0, sock_read=65.0)

    for attempt in range(max_retries):
        api_key = await key_manager.acquire_key()
        if not api_key:
            logger.error("No valid NVIDIA NIM API key obtained from key manager.")
            break

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        try:
            async with session.post(target_endpoint, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        content = msg.get("content") or msg.get("reasoning_content") or ""
                        usage = data.get("usage", {})
                        return LLMResult(
                            text=content.strip(),
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            candidate_tokens=usage.get("completion_tokens", 0),
                            success=True
                        )
                    else:
                        logger.warning(f"NIM API 200 OK but empty choices array: {data}")
                        return LLMResult(text="Error", success=False)

                elif resp.status == 429:
                    key_manager.mark_rate_limited(api_key, penalty_sec=60.0)
                    logger.warning(f"NIM API 429 Rate Limit on attempt {attempt + 1}. Retrying with next key...")
                    continue

                elif resp.status in (401, 403):
                    key_manager.mark_invalid(api_key)
                    logger.warning(f"NIM API {resp.status} Unauthorized/Forbidden on attempt {attempt + 1}. Deactivated key.")
                    continue

                else:
                    err_text = await resp.text()
                    logger.error(f"NIM API HTTP {resp.status} error: {err_text[:200]}")
                    key_manager.mark_rate_limited(api_key, penalty_sec=5.0)

        except asyncio.TimeoutError:
            logger.warning(f"NIM API timeout on attempt {attempt + 1}. Cooling down key for 10s...")
            key_manager.mark_rate_limited(api_key, penalty_sec=10.0)
        except Exception as e:
            logger.error(f"NIM API request exception on attempt {attempt + 1}: {e}")
            key_manager.mark_rate_limited(api_key, penalty_sec=5.0)

        await asyncio.sleep(0.5)

    return LLMResult(text="Error", success=False)
