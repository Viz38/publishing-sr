import pytest
import asyncio
import time
from sr_common.clients import MultiTierRateLimiter

@pytest.mark.asyncio
async def test_rate_limiter_basic():
    limits = {'second': 5, 'minute': 10}
    limiter = MultiTierRateLimiter(db_path=":memory:", limits=limits)
    
    start_time = time.time()
    
    # Send 5 requests (should pass immediately)
    for _ in range(5):
        await limiter.throttle()
        
    elapsed_first_batch = time.time() - start_time
    assert elapsed_first_batch < 0.2, "First 5 requests should be immediate"
    
    # 6th request should block until a second passes
    await limiter.throttle()
    elapsed_second_batch = time.time() - start_time
    assert elapsed_second_batch >= 1.0, "6th request must wait for the 1-second window to clear"

@pytest.mark.asyncio
async def test_rate_limiter_eviction():
    limits = {'second': 2}
    limiter = MultiTierRateLimiter(db_path=":memory:", limits=limits)
    
    await limiter.throttle()
    await limiter.throttle()
    
    # Wait for eviction
    await asyncio.sleep(1.1)
    
    start_time = time.time()
    await limiter.throttle()
    elapsed = time.time() - start_time
    assert elapsed < 0.1, "Request should be immediate after eviction"
