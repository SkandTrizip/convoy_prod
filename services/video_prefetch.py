"""Feed-scroll prefetch caching: warms the next few videos' first segment in
Redis while the current one is being watched, via a RabbitMQ job queue
consumed by worker.py.

Pure performance optimization — if Redis/RabbitMQ are unavailable, every
function here still returns a correct result, just by fetching straight from
Bunny each time (see services/redis_cache.py's degrade-not-raise contract).
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from sqlalchemy.ext.asyncio import AsyncSession

from config import PREFETCH_CACHE_TTL_SECONDS, PREFETCH_LOCK_TTL_SECONDS, PREFETCH_NEIGHBOR_WINDOW, PREFETCH_SEGMENT_COUNT, logger
from services import bunny_stream, rabbitmq, redis_cache, videos


_TAG_URI_RE = re.compile(r'URI="([^"]+)"')


def _rewrite_tag_uri(line: str, variant_url: str) -> str:
    """Rewrites a relative URI inside a tag line's URI="..." attribute (e.g.
    #EXT-X-MAP, #EXT-X-KEY, used by fMP4 playlists) to an absolute Bunny CDN
    URL. Without this, a tag like `#EXT-X-MAP:URI="init.mp4"` would be left
    relative, and since the manifest is served from our own API path (not
    Bunny's), the player would resolve it against our host instead - a 404,
    since no such route exists here."""
    match = _TAG_URI_RE.search(line)
    if not match:
        return line
    absolute_uri = urljoin(variant_url, match.group(1))
    return line[: match.start(1)] + absolute_uri + line[match.end(1) :]


def content_key(bunny_video_id: str, segment_index: int) -> str:
    return f"prefetch:seg:{bunny_video_id}:{segment_index}"


def lock_key(bunny_video_id: str, segment_index: int) -> str:
    return f"prefetch:lock:{bunny_video_id}:{segment_index}"


@dataclass
class WatchingResult:
    neighbors_checked: int
    cache_hits: int
    jobs_published: int


@dataclass
class PreviewSegment:
    data: bytes
    source: str  # "cache" or "bunny" - which path actually served these bytes


async def report_watching(session: AsyncSession, video_id: uuid.UUID, feed_position: int) -> WatchingResult:
    await videos.get_video(session, video_id)  # 404s if missing

    window_size = 2 * PREFETCH_NEIGHBOR_WINDOW + 1
    window_offset = max(0, feed_position - PREFETCH_NEIGHBOR_WINDOW)
    window, _ = await videos.list_feed(session, limit=window_size, offset=window_offset)
    neighbors = [v for v in window if v.id != video_id]

    cache_hits = 0
    jobs_published = 0
    for neighbor in neighbors:
        for segment_index in range(PREFETCH_SEGMENT_COUNT):
            if await redis_cache.get(content_key(neighbor.bunny_video_id, segment_index)) is not None:
                cache_hits += 1
                continue

            acquired = await redis_cache.try_acquire_lock(
                lock_key(neighbor.bunny_video_id, segment_index), PREFETCH_LOCK_TTL_SECONDS
            )
            if not acquired:
                continue

            published = await rabbitmq.publish_prefetch_job(
                {
                    "bunny_video_id": neighbor.bunny_video_id,
                    "segment_index": segment_index,
                    "video_id": str(neighbor.id),
                    "enqueued_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            if published:
                jobs_published += 1
            else:
                await redis_cache.delete(lock_key(neighbor.bunny_video_id, segment_index))

    return WatchingResult(
        neighbors_checked=len(neighbors), cache_hits=cache_hits, jobs_published=jobs_published
    )


async def get_preview_segment(session: AsyncSession, video_id: uuid.UUID) -> PreviewSegment:
    video = await videos.get_video(session, video_id)

    key = content_key(video.bunny_video_id, 0)
    cached = await redis_cache.get(key)
    if cached is not None:
        logger.info("preview-segment CACHE HIT video_id=%s bunny_video_id=%s", video_id, video.bunny_video_id)
        return PreviewSegment(data=cached, source="cache")

    logger.info(
        "preview-segment CACHE MISS video_id=%s bunny_video_id=%s - fetching from Bunny",
        video_id,
        video.bunny_video_id,
    )
    segment_bytes = await bunny_stream.fetch_segment_bytes(video.bunny_video_id, 0)
    await redis_cache.set(key, segment_bytes, PREFETCH_CACHE_TTL_SECONDS)
    return PreviewSegment(data=segment_bytes, source="bunny")


async def get_fast_start_manifest(session: AsyncSession, video_id: uuid.UUID) -> str:
    """Builds an HLS media playlist byte-identical to Bunny's real one, except
    segment 0 points at our own preview-segment endpoint (cache-or-Bunny) and
    every later segment points at Bunny's CDN directly. A standard HLS player
    given this URL just plays the video normally - segment 0 loads fast when
    it's been prefetched, everything after streams straight from Bunny.
    """
    video = await videos.get_video(session, video_id)
    variant_text, variant_url = await bunny_stream.fetch_variant_playlist(video.bunny_video_id)

    rewritten_lines = []
    first_segment_seen = False
    for line in variant_text.splitlines():
        if line and not line.startswith("#"):
            if not first_segment_seen:
                rewritten_lines.append(f"/api/videos/{video_id}/preview-segment")
                first_segment_seen = True
            else:
                rewritten_lines.append(urljoin(variant_url, line))
        else:
            rewritten_lines.append(_rewrite_tag_uri(line, variant_url))

    return "\n".join(rewritten_lines)
