"""Best-effort Redis cache/lock for video-segment prefetching.

Deliberately degrades rather than raises: a missing REDIS_URL or an
unreachable Redis instance just means "nothing is cached, nothing is locked"
(services/video_prefetch.py falls straight back to fetching from Bunny) — it
must never break video playback itself, matching this repo's existing
fire-and-forget pattern for non-critical paths (e.g. push notifications,
activity logging).
"""

from functools import lru_cache

from redis.asyncio import Redis

from config import REDIS_URL, logger


@lru_cache(maxsize=1)
def _client() -> Redis | None:
    if not REDIS_URL:
        return None
    return Redis.from_url(REDIS_URL)


async def get(key: str) -> bytes | None:
    client = _client()
    if client is None:
        return None
    try:
        return await client.get(key)
    except Exception as e:
        logger.warning("Redis get failed for %s: %s", key, e)
        return None


async def set(key: str, value: bytes, ttl_seconds: int) -> None:
    client = _client()
    if client is None:
        return
    try:
        await client.set(key, value, ex=ttl_seconds)
    except Exception as e:
        logger.warning("Redis set failed for %s: %s", key, e)


async def try_acquire_lock(key: str, ttl_seconds: int) -> bool:
    client = _client()
    if client is None:
        return False
    try:
        return bool(await client.set(key, b"1", ex=ttl_seconds, nx=True))
    except Exception as e:
        logger.warning("Redis lock attempt failed for %s: %s", key, e)
        return False


async def delete(key: str) -> None:
    client = _client()
    if client is None:
        return
    try:
        await client.delete(key)
    except Exception as e:
        logger.warning("Redis delete failed for %s: %s", key, e)


async def hincrby(name: str, field: str, amount: int = 1) -> bool:
    """True if the increment was recorded; False means Redis is unavailable and
    the caller should fall back to its non-Redis path."""
    client = _client()
    if client is None:
        return False
    try:
        await client.hincrby(name, field, amount)
        return True
    except Exception as e:
        logger.warning("Redis hincrby failed for %s: %s", name, e)
        return False


async def claim(key: str, ttl_seconds: int) -> bool | None:
    """SET NX with TTL. True = newly claimed, False = already claimed,
    None = Redis unavailable (unlike try_acquire_lock, which can't tell the
    last two apart)."""
    client = _client()
    if client is None:
        return None
    try:
        return bool(await client.set(key, b"1", ex=ttl_seconds, nx=True))
    except Exception as e:
        logger.warning("Redis claim failed for %s: %s", key, e)
        return None


async def drain_hash(name: str, processing_name: str) -> dict[str, int] | None:
    """Atomically takes everything accumulated in hash `name`.

    RENAME is atomic, so concurrent drainers (several API instances) never take
    the same data, and writers keep incrementing a fresh `name` meanwhile. If a
    previous drain crashed before `delete(processing_name)`, that leftover is
    returned first instead. Returns None if Redis is unavailable. The caller
    must `delete(processing_name)` once it has durably applied the result.
    """
    client = _client()
    if client is None:
        return None
    try:
        if not await client.exists(processing_name):
            try:
                await client.rename(name, processing_name)
            except Exception as e:
                if "no such key" in str(e).lower():
                    return {}
                raise
        raw = await client.hgetall(processing_name)
        return {k.decode(): int(v) for k, v in raw.items()}
    except Exception as e:
        logger.warning("Redis drain failed for %s: %s", name, e)
        return None
