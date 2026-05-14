import pytest
import asyncio
from sr_common.fetcher import StealthFetcher

@pytest.mark.asyncio
async def test_fetcher_tier0_success():
    fetcher = StealthFetcher()
    # Test with a simple site that usually allows curl
    content, status, msg = await fetcher.fetch(None, "https://example.com")
    
    assert status == 200
    assert "Example Domain" in content
    assert msg == "Success"

def test_fetcher_is_valid():
    fetcher = StealthFetcher()
    assert fetcher._is_valid("valid content " * 100) is True
    assert fetcher._is_valid("sgcaptcha") is False
    assert fetcher._is_valid("") is False
    assert fetcher._is_valid("too short", min_len=100) is False
