from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List

class RunRequest(BaseModel):
    start_row: int = 10
    mode: str = "full"
    sheet_id: Optional[str] = None
    apply_formatting: bool = True

class ProgressState(BaseModel):
    current: int = 0
    total: int = 0
    success: int = 0
    fail: int = 0

class LLMResult(BaseModel):
    text: str
    prompt_tokens: int = 0
    candidate_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    thinking_text: str = ""
    success: bool = True

class APIResponse(BaseModel):
    status: str
    message: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
