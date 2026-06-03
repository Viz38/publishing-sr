import asyncio
import time
from sr_common.clients import RateLimiter, MultiTierRateLimiter
import os

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Logs')
os.makedirs(LOGS_DIR, exist_ok=True)

async def mock_api_call(delay=0.5):
    await asyncio.sleep(delay)
    return 200, {"status": "ok"}

async def run_sequential():
    start = time.time()
    await mock_api_call() # DP
    await mock_api_call() # BM
    await mock_api_call() # Funnel Force Assign
    await mock_api_call() # Funnel Move
    return time.time() - start

async def run_parallel():
    start = time.time()
    
    async def update_dp():
        return await mock_api_call()
        
    async def update_bm():
        return await mock_api_call()
        
    async def update_funnel():
        await mock_api_call() # Force Assign
        await mock_api_call() # Move
        return 200
        
    await asyncio.gather(update_dp(), update_bm(), update_funnel())
    return time.time() - start

async def run_gemini_sequential():
    start = time.time()
    await mock_api_call(2.0) # Prompt 1
    await mock_api_call(2.0) # Prompt 2
    return time.time() - start

async def run_gemini_parallel():
    start = time.time()
    await asyncio.gather(mock_api_call(2.0), mock_api_call(2.0))
    return time.time() - start

async def main():
    print("--- Tracxn API Update Benchmark ---")
    seq_time = await run_sequential()
    print(f"Sequential API Updates: {seq_time:.2f} seconds")
    
    par_time = await run_parallel()
    print(f"Parallel API Updates:   {par_time:.2f} seconds")
    
    print(f"Improvement: {seq_time - par_time:.2f} seconds saved per domain ({(seq_time - par_time)/seq_time*100:.1f}% faster)\n")
    
    print("--- Gemini LLM Call Benchmark ---")
    seq_gem = await run_gemini_sequential()
    print(f"Sequential Gemini Calls: {seq_gem:.2f} seconds")
    
    par_gem = await run_gemini_parallel()
    print(f"Parallel Gemini Calls:   {par_gem:.2f} seconds")
    
    print(f"Improvement: {seq_gem - par_gem:.2f} seconds saved per domain ({(seq_gem - par_gem)/seq_gem*100:.1f}% faster)\n")

    print("--- Rate Limiter Initialization ---")
    db_path = os.path.join(LOGS_DIR, 'test_rate_limit.db')
    if os.path.exists(db_path):
        os.remove(db_path)
    if os.path.exists(db_path + "-wal"):
        os.remove(db_path + "-wal")
        
    limiter = MultiTierRateLimiter(db_path, {'second': 100, 'minute': 1000, 'hour': 10000, 'day': 100000})
    print("MultiTierRateLimiter successfully initialized with strict limits.")
    
if __name__ == "__main__":
    asyncio.run(main())
