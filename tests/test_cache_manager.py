import pytest
import asyncio
import time
import aiohttp
from unittest.mock import AsyncMock, MagicMock, patch
from sr_common.utils import GeminiCacheManager

@pytest.mark.asyncio
async def test_cache_manager_creation():
    manager = GeminiCacheManager("dummy_key", max_size=2)
    session = AsyncMock(spec=aiohttp.ClientSession)
    
    # Mock post response
    post_response = AsyncMock()
    post_response.status = 200
    post_response.json = AsyncMock(return_value={"name": "cachedContents/test-123"})
    session.post.return_value.__aenter__.return_value = post_response
    
    # Call get_or_create
    cache_id = await manager.get_or_create(session, "prompt_1", "sys_instruct", ttl="3600s")
    
    assert cache_id == "cachedContents/test-123"
    assert "prompt_1" in manager.caches
    assert session.post.call_count == 1

@pytest.mark.asyncio
async def test_cache_manager_expiry():
    manager = GeminiCacheManager("dummy_key")
    session = AsyncMock(spec=aiohttp.ClientSession)
    
    # First call - creates cache
    post_response1 = AsyncMock()
    post_response1.status = 200
    post_response1.json = AsyncMock(return_value={"name": "cachedContents/exp-1"})
    
    # Second call - recreating cache due to expiry
    post_response2 = AsyncMock()
    post_response2.status = 200
    post_response2.json = AsyncMock(return_value={"name": "cachedContents/exp-2"})
    
    session.post.return_value.__aenter__.side_effect = [post_response1, post_response2]
    
    # Capture real time before mocking
    real_time = time.time()
    
    # 1. Create cache
    with patch('sr_common.utils.time.time', return_value=real_time):
        await manager.get_or_create(session, "prompt_exp", "sys", ttl="3600s")
    
    # 2. Fast forward time to simulate expiry (beyond the 5 min buffer)
    with patch('sr_common.utils.time.time') as mock_time:
        mock_time.return_value = real_time + 3400
        # This should trigger a new creation
        cache_id2 = await manager.get_or_create(session, "prompt_exp", "sys", ttl="3600s")
        
    assert cache_id2 == "cachedContents/exp-2"
    assert session.post.call_count == 2

@pytest.mark.asyncio
async def test_cache_manager_evicts_closest_to_expiry():
    manager = GeminiCacheManager("dummy_key", max_size=2)
    session = AsyncMock(spec=aiohttp.ClientSession)
    
    def create_mock_post(name):
        resp = AsyncMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={"name": name})
        return resp
        
    session.post.return_value.__aenter__.side_effect = [
        create_mock_post("cachedContents/c1"),
        create_mock_post("cachedContents/c2"),
        create_mock_post("cachedContents/c3")
    ]
    
    delete_response = AsyncMock()
    delete_response.status = 200
    session.delete.return_value.__aenter__.return_value = delete_response
    
    real_time = time.time()
    
    with patch('sr_common.utils.time.time', return_value=real_time):
        # Add 2 items (max_size is 2)
        await manager.get_or_create(session, "key1", "sys", ttl="3600s")
    
    with patch('sr_common.utils.time.time', return_value=real_time + 100):
        await manager.get_or_create(session, "key2", "sys", ttl="3600s")
    
    assert len(manager.caches) == 2
    
    # Access key1 again, doesn't change its expiry
    with patch('sr_common.utils.time.time', return_value=real_time + 200):
        await manager.get_or_create(session, "key1", "sys", ttl="3600s")
    
    # Add a 3rd item, which should evict key1 since its expiry (real_time + 3600) 
    # is smaller than key2's expiry (real_time + 100 + 3600)
    with patch('sr_common.utils.time.time', return_value=real_time + 300):
        await manager.get_or_create(session, "key3", "sys", ttl="3600s")
    
    assert len(manager.caches) == 2
    assert "key1" not in manager.caches
    assert "key2" in manager.caches
    assert "key3" in manager.caches
    
    # Verify the API was called to delete the evicted cache
    session.delete.assert_called_once()
    deleted_url = session.delete.call_args[0][0]
    assert "cachedContents/c1" in deleted_url
