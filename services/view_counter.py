"""Reel view counting, built for write volume.

A view is the hottest write in the reels feature (every viewer x every reel),
so it never touches Postgres on the request path: `record_view` does one O(1)
Redis HINCRBY into a single pending hash, and `flush_pending_views` (run on a
timer, see `run_view_flush_loop`) drains that hash atomically and applies each
reel's delta to `videos.view_count` in one batched statement. A viral reel is
therefore one UPDATE per flush interval instead of one row-lock per view.

Trade-offs, deliberate:
- Counts lag by up to REEL_VIEW_FLUSH_INTERVAL_SECONDS.
- Delivery is at-least-once: a crash between applying a batch and deleting the
  drained hash re-applies that batch on restart (tiny over-count, never a loss).
- If Redis is unavailable, `record_view` falls back to a direct atomic UPDATE,
  so views are never dropped — just slower for that period.

Everything callers need is `record_view` / `flush_pending_views`; to move to a
different backend later (Redis streams, Kafka, ClickHouse), change this module
only.
"""

import asyncio
import uuid

from sqlalchemy import bindparam, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import REEL_VIEW_DEDUPE_SECONDS, REEL_VIEW_FLUSH_INTERVAL_SECONDS, logger
from db.base import Video
from services import redis_cache

PENDING_KEY = "reel:views:pending"
PROCESSING_KEY = "reel:views:processing"
FLUSH_BATCH_SIZE = 1000


def _dedupe_key(video_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"reel:views:seen:{video_id}:{user_id}"


async def record_view(session: AsyncSession, video: Video, viewer_id: uuid.UUID) -> bool:
    """Returns True if this report counted, False if ignored.

    Ignored: the reel's own creator, and the same viewer re-reporting the same
    reel inside the dedupe window. Callers have already checked the reel is
    ready.
    """
    if viewer_id == video.user_id:
        return False

    claimed = await redis_cache.claim(_dedupe_key(video.id, viewer_id), REEL_VIEW_DEDUPE_SECONDS)
    if claimed is False:
        return False

    if claimed is True and await redis_cache.hincrby(PENDING_KEY, str(video.id), 1):
        return True

    # Redis unavailable (or died between the two calls): count it directly.
    await session.execute(update(Video).where(Video.id == video.id).values(view_count=Video.view_count + 1))
    await session.commit()
    return True


async def flush_pending_views() -> int:
    """Applies buffered views to Postgres. Safe to run from several processes
    at once (see redis_cache.drain_hash). Returns the number of views applied."""
    from db import async_session

    drained = await redis_cache.drain_hash(PENDING_KEY, PROCESSING_KEY)
    if not drained:
        return 0

    rows = []
    for raw_id, delta in drained.items():
        try:
            rows.append({"b_id": uuid.UUID(raw_id), "b_n": delta})
        except ValueError:
            logger.warning("Dropping malformed video id in pending views: %r", raw_id)
    rows.sort(key=lambda r: r["b_id"])  # stable lock order

    table = Video.__table__
    stmt = (
        update(table)
        .where(table.c.id == bindparam("b_id"))
        .values(view_count=table.c.view_count + bindparam("b_n"))
    )
    async with async_session() as session:
        for i in range(0, len(rows), FLUSH_BATCH_SIZE):
            await session.execute(stmt, rows[i : i + FLUSH_BATCH_SIZE])
        await session.commit()

    await redis_cache.delete(PROCESSING_KEY)
    return sum(r["b_n"] for r in rows)


async def run_view_flush_loop() -> None:
    """Background job started from app.py's lifespan. Every API instance runs
    one; the atomic drain makes that safe."""
    while True:
        try:
            applied = await flush_pending_views()
            if applied:
                logger.info("Flushed %s buffered reel view(s)", applied)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Reel view flush failed: %s", e, exc_info=True)
        await asyncio.sleep(REEL_VIEW_FLUSH_INTERVAL_SECONDS)
