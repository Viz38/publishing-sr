import pytest
import asyncio
import time
import aiohttp
import os
import json
from unittest.mock import AsyncMock, MagicMock, patch
from TypeB.main import TrackingCacheManager

@pytest.mark.asyncio
async def test_tracking_cache_manager(tmp_path):
    stats_file = str(tmp_path / "cache_stats.json")
    manager = TrackingCacheManager("dummy_key", max_size=2, stats_filepath=stats_file)
    session = AsyncMock(spec=aiohttp.ClientSession)
    
    # Mock post response
    post_response1 = AsyncMock()
    post_response1.status = 200
    post_response1.json = AsyncMock(return_value={"name": "cachedContents/test-1"})
    
    post_response2 = AsyncMock()
    post_response2.status = 200
    post_response2.json = AsyncMock(return_value={"name": "cachedContents/test-2"})
    
    # Mock get response for remote sync (empty)
    get_response = AsyncMock()
    get_response.status = 200
    get_response.json = AsyncMock(return_value={"cachedContents": []})
    session.get.return_value.__aenter__.return_value = get_response
    
    session.post.return_value.__aenter__.side_effect = [post_response1, post_response2]
    
    # 1. Create first cache
    cache_id1 = await manager.get_or_create(session, "prompt_1", "sys_1", ttl="3600s")
    assert cache_id1 == "cachedContents/test-1"
    
    # Verify stats
    assert os.path.exists(stats_file)
    with open(stats_file, "r") as f:
        data = json.load(f)
    assert data["summary"]["total_created"] == 1
    assert data["summary"]["total_used"] == 0
    assert len(data["caches"]) == 1
    assert data["caches"][0]["key"] == "prompt_1"
    assert data["caches"][0]["cache_id"] == "cachedContents/test-1"
    assert data["caches"][0]["expiry_status"] == "live"
    assert data["caches"][0]["used_count"] == 0
    
    # 2. Reuse first cache (hits fast path)
    cache_id1_dup = await manager.get_or_create(session, "prompt_1", "sys_1", ttl="3600s")
    assert cache_id1_dup == "cachedContents/test-1"
    
    with open(stats_file, "r") as f:
        data = json.load(f)
    assert data["summary"]["total_created"] == 1
    assert data["summary"]["total_used"] == 1
    assert data["caches"][0]["used_count"] == 1

    # 3. Create second cache
    cache_id2 = await manager.get_or_create(session, "prompt_2", "sys_2", ttl="3600s")
    assert cache_id2 == "cachedContents/test-2"
    
    with open(stats_file, "r") as f:
        data = json.load(f)
    assert data["summary"]["total_created"] == 2
    assert data["summary"]["total_used"] == 1
    
    # Find prompt_2 in the list
    prompt2_info = next(item for item in data["caches"] if item["key"] == "prompt_2")
    assert prompt2_info["used_count"] == 0

    # 4. Invalidate cache
    await manager.invalidate("prompt_1")
    with open(stats_file, "r") as f:
        data = json.load(f)
    prompt1_info = next(item for item in data["caches"] if item["key"] == "prompt_1")
    assert prompt1_info["expiry_status"] == "expired"
