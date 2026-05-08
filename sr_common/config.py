import os
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # API Keys & Tokens
    TYPEA_GEMINI_API_KEY: str
    TYPEB_GEMINI_API_KEY: str
    TYPEC_GEMINI_API_KEY: str
    
    TYPEA_TRACXN_ACCESS_TOKEN: str
    TYPEB_TRACXN_ACCESS_TOKEN: str
    TYPEC_TRACXN_ACCESS_TOKEN: str
    
    # Sheet IDs
    TYPEA_SHEET_ID: str
    TYPEB_SHEET_ID: str
    TYPEC_SHEET_ID: str
    
    MASTER_SHEET_ID: str
    PROMPTS_SHEET_ID: str
    FEED_OWNER_SHEET_ID: str
    FEED_DEF_SHEET_ID_1: str
    FEED_DEF_SHEET_ID_2: str
    
    # Engine Settings
    GEMINI_API_URL: str = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    MAX_PROMPT_SIZE: int = 40000
    BATCH_SIZE: int = 5
    REQUEST_TIMEOUT: int = 30
    MAX_RETRIES: int = 10
    RETRY_DELAY: int = 5
    
    # Service Auth
    SERVICE_AUTH_TOKEN: str

    model_config = {
        "env_file": os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        "extra": "ignore"
    }

settings = Settings()
