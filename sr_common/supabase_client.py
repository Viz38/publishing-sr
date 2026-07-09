"""
sr_common/supabase_client.py
Async PostgreSQL client for the TypeB Tech Crawler data source.
Queries the Supabase scraped_data table for pre-fetched page content.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger("sr_common.supabase_client")

_pool = None
_pool_lock = asyncio.Lock()


async def _get_pool():
    """Return the shared asyncpg connection pool, creating it if necessary."""
    global _pool
    if _pool is not None:
        return _pool

    async with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            import asyncpg
            from .config import settings

            _pool = await asyncpg.create_pool(
                host=settings.SUPABASE_HOST,
                port=settings.SUPABASE_PORT,
                database=settings.SUPABASE_DB,
                user=settings.SUPABASE_USER,
                password=settings.SUPABASE_PASSWORD,
                min_size=1,
                max_size=5,
                command_timeout=10,
                ssl="require",
                statement_cache_size=0,
            )
            logger.info("SUPABASE: Connection pool created successfully")
        except Exception as e:
            logger.error(f"SUPABASE: Failed to create connection pool — {e}")
            print(f"SUPABASE ERROR CRITICAL: Failed to create connection pool — {e}")
            _pool = None

    return _pool


async def fetch_scraped_content(domain: str) -> Optional[str]:
    """
    Look up pre-fetched content for domain in the Supabase scraped_data table.
    Normalises domain (strips https://, http://, trailing slashes).
    Returns content string or None if not found / DB error.
    """
    clean = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
    clean = clean.rstrip("/")

    try:
        pool = await _get_pool()
        if pool is None:
            logger.warning(f"SUPABASE: Pool unavailable, skipping lookup for {domain}")
            return None

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT home_about_content FROM scraped_data WHERE domain = $1 LIMIT 1",
                clean,
            )

        if row and row["home_about_content"] and row["home_about_content"].strip():
            content = row["home_about_content"].strip()
            if len(content) >= 300:
                logger.info(f"SUPABASE HIT: {domain} ({len(content)} chars)")
                return content
            else:
                logger.info(f"SUPABASE MISS: {domain} — content too short ({len(content)} chars)")
                return None

        logger.info(f"SUPABASE MISS: {domain} — no content found")
        return None

    except Exception as e:
        logger.warning(f"SUPABASE ERR: {domain} — {e}")
        return None


async def close_pool():
    """Gracefully close the connection pool on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("SUPABASE: Connection pool closed")
