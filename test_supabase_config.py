import asyncio
from unittest.mock import patch, AsyncMock
from sr_common.config import settings
import sr_common.supabase_client

async def test_supabase_pool_uses_correct_password():
    # Force the pool to be None so it tries to create a new one
    sr_common.supabase_client._pool = None
    
    with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create_pool:
        # Mock it to return a dummy string just so we know it succeeded
        mock_create_pool.return_value = "dummy_pool"
        
        pool = await sr_common.supabase_client._get_pool()
        
        # Verify it was called exactly once
        mock_create_pool.assert_called_once()
        
        # Get the kwargs that create_pool was called with
        _, kwargs = mock_create_pool.call_args
        
        # The test: Verify it used the password from settings
        assert kwargs["password"] == "Tracxn@12234", f"Password does not match expected! Got: {kwargs['password']}"
        
        print(f"✅ GREEN: Password used in pool creation is exactly: {kwargs['password']}")

if __name__ == "__main__":
    asyncio.run(test_supabase_pool_uses_correct_password())
