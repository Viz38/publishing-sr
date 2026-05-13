import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# Discovery: Find .env in the project root
_current_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.dirname(_current_dir)
_env_path = os.path.join(_root_dir, ".env")

# Debugging: This will show up in api.logs
print(f"DEBUG: Config loading from root: {_root_dir}")
print(f"DEBUG: Env file path: {_env_path}")
print(f"DEBUG: Env file exists: {os.path.exists(_env_path)}")

# Manually load the .env file into os.environ
if os.path.exists(_env_path):
    load_dotenv(_env_path, override=True)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_path,
        env_file_encoding='utf-8',
        extra="ignore"
    )

    # API Keys & Tokens - Using literal defaults to bypass Pydantic requirement
    TYPEA_GEMINI_API_KEY: str = ""
    TYPEB_GEMINI_API_KEY: str = ""
    TYPEC_GEMINI_API_KEY: str = ""
    
    TYPEA_TRACXN_ACCESS_TOKEN: str = ""
    TYPEB_TRACXN_ACCESS_TOKEN: str = ""
    TYPEC_TRACXN_ACCESS_TOKEN: str = ""
    
    # Sheet IDs
    TYPEA_SHEET_ID: str = ""
    TYPEB_SHEET_ID: str = ""
    TYPEC_SHEET_ID: str = ""
    
    MASTER_SHEET_ID: str = ""
    PROMPTS_SHEET_ID: str = ""
    FEED_OWNER_SHEET_ID: str = ""
    FEED_DEF_SHEET_ID_1: str = ""
    FEED_DEF_SHEET_ID_2: str = ""
    
    # Engine Settings
    GEMINI_API_URL: str = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash:generateContent"
    GEMINI_CACHE_URL: str = "https://generativelanguage.googleapis.com/v1beta/cachedContents"
    MAX_PROMPT_SIZE: int = 40000
    BATCH_SIZE: int = 10
    REQUEST_TIMEOUT: int = 90
    MAX_RETRIES: int = 10
    RETRY_DELAY: int = 5
    CONFIGURED_MAX_WORKERS: int = 15
    CONFIGURED_MIN_WORKERS: int = 1
    
    # Service Auth
    SERVICE_AUTH_TOKEN: str = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Manually sync from environ if Pydantic missed them
        for field in self.model_fields:
            env_val = os.getenv(field)
            if env_val and not getattr(self, field):
                setattr(self, field, env_val)

settings = Settings()
