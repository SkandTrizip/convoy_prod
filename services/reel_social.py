"""Reel metadata (categories, hashtags, posting location) and social actions
(likes, comments, shares).

Counters on `videos` (like_count/comment_count/share_count) are denormalized
for cheap feed reads. They only move when the underlying row insert/delete
actually happened (INSERT ... ON CONFLICT DO NOTHING RETURNING / DELETE ...
RETURNING), and the increment itself is a single atomic UPDATE, so concurrent
likes from many users never lose updates.
"""

import asyncio
import re
import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from db.base import ReelCategory, User, Video, VideoCategory, VideoComment, VideoLike, VideoShare
from db.serializers import reel_to_dict
from services import follows, google_places
from services.geo import calculate_distance
from services.spatial import make_geography_point

MAX_CATEGORIES_PER_REEL = 5
MAX_HASHTAGS_PER_REEL = 10
# Nearest known `locations` row within this distance is used as the label when
# Google reverse geocoding is unavailable.
LOCATION_FALLBACK_RADIUS_KM = 30.0

_HASHTAG_RE = re.compile(r"^[a-z0-9_]{1,50}$")


def normalize_hashtags(raw: list[str] | None) -> list[str]:
    """Lowercase, strip leading '#', dedupe (order kept). Rejects anything that
    isn't letters/digits/underscore so tags stay URL- and filter-safe."""
    if not raw:
        return []
    seen: dict[str, None] = {}
    for tag in raw:
        cleaned = tag.strip().lstrip("#").lower()
        if not cleaned:
            continue
        if not _HASHTAG_RE.match(cleaned):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid hashtag '{tag}': use 1-50 letters, digits or underscores",
            )
        seen[cleaned] = None
    if len(seen) > MAX_HASHTAGS_PER_REEL:
        raise HTTPException(status_code=400, detail=f"At most {MAX_HASHTAGS_PER_REEL} hashtags allowed")
    return list(seen)


async def resolve_categories(session: AsyncSession, category_ids: list[str]) -> list[ReelCategory]:
    try:
        wanted = list(dict.fromkeys(uuid.UUID(cid) for cid in category_ids))
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid category id") from e
    if not 1 <= len(wanted) <= MAX_CATEGORIES_PER_REEL:
        raise HTTPException(
            status_code=400, detail=f"Pick between 1 and {MAX_CATEGORIES_PER_REEL} categories"
        )

    result = await session.execute(
        select(ReelCategory).where(ReelCategory.id.in_(wanted), ReelCategory.is_active.is_(True))
    )
    found = {c.id: c for c in result.scalars().all()}
    missing = [str(cid) for cid in wanted if cid not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown or inactive category: {', '.join(missing)}")
    return [found[cid] for cid in wanted]


async def _nearest_known_place(session: AsyncSession, lat: float, lng: float) -> dict | None:
    # Bounding-box prefilter (~50km), then the exact distance check below.
    row = (
        await session.execute(
            text(
                """
                WITH p AS (SELECT CAST(:lat AS double precision) AS lat, CAST(:lng AS double precision) AS lng)
                SELECT l.city, l.state, l.lat, l.lng FROM locations l, p
                WHERE l.city IS NOT NULL
                  AND l.lat BETWEEN p.lat - 0.5 AND p.lat + 0.5
                  AND l.lng BETWEEN p.lng - 0.5 AND p.lng + 0.5
                ORDER BY (l.lat - p.lat) ^ 2 + ((l.lng - p.lng) * cos(radians(p.lat))) ^ 2
                LIMIT 1
                """
            ),
            {"lat": lat, "lng": lng},
        )
    ).first()
    if row is None or calculate_distance(lat, lng, row.lat, row.lng) > LOCATION_FALLBACK_RADIUS_KM:
        return None
    return {"city": row.city, "state": row.state}


async def apply_location(session: AsyncSession, video: Video, lat: float, lng: float) -> None:
    """Stores the GPS point and a city/state label. Labeling is best-effort:
    a reel is never rejected because geocoding failed."""
    video.location = make_geography_point(lng, lat)
    place = None
    try:
        place = await asyncio.to_thread(google_places.fetch_reverse_geocode, lat, lng)
    except Exception as e:
        logger.error(f"Reverse geocode failed for video {video.id}: {e}")
    if not place or not place.get("city"):
        place = await _nearest_known_place(session, lat, lng) or place
    if place:
        video.location_city = place.get("city")
        video.location_state = place.get("state")


def set_video_categories(session: AsyncSession, video_id: uuid.UUID, categories: list[ReelCategory]) -> None:
    session.add_all(VideoCategory(video_id=video_id, category_id=c.id) for c in categories)


async def get_ready_video(session: AsyncSession, video_id: uuid.UUID) -> Video:
    """Social actions only make sense on published reels."""
    video = await session.get(Video, video_id)
    if video is None or video.status != "ready":
        raise HTTPException(status_code=404, detail="Reel not found")
    return video


# --- Reading context for serialization -------------------------------------


@dataclass
class ReelContext:
    authors: dict[uuid.UUID, User] = field(default_factory=dict)
    categories: dict[uuid.UUID, list[ReelCategory]] = field(default_factory=dict)
    liked: set[uuid.UUID] = field(default_factory=set)
    followed_authors: set[uuid.UUID] = field(default_factory=set)
    author_follower_counts: dict[uuid.UUID, int] = field(default_factory=dict)


async def load_reel_context(session: AsyncSession, videos: list[Video], viewer_id: uuid.UUID) -> ReelContext:
    """Batch-loads everything reel_to_dict needs in 5 queries, regardless of
    page size."""
    ctx = ReelContext()
    if not videos:
        return ctx
    video_ids = [v.id for v in videos]

    authors = await session.execute(select(User).where(User.id.in_({v.user_id for v in videos})))
    ctx.authors = {u.id: u for u in authors.scalars().all()}

    cats = await session.execute(
        select(VideoCategory.video_id, ReelCategory)
        .join(ReelCategory, ReelCategory.id == VideoCategory.category_id)
        .where(VideoCategory.video_id.in_(video_ids))
        .order_by(ReelCategory.sort_order, ReelCategory.name)
    )
    for video_id, category in cats.all():
        ctx.categories.setdefault(video_id, []).append(category)

    liked = await session.execute(
        select(VideoLike.video_id).where(VideoLike.user_id == viewer_id, VideoLike.video_id.in_(video_ids))
    )
    ctx.liked = set(liked.scalars().all())

    ctx.followed_authors, ctx.author_follower_counts = await follows.author_follow_context(
        session, set(ctx.authors), viewer_id
    )
    return ctx


async def serialize_reels(session: AsyncSession, videos: list[Video], viewer_id: uuid.UUID) -> list[dict]:
    ctx = await load_reel_context(session, videos, viewer_id)
    return [
        reel_to_dict(
            v,
            ctx.authors.get(v.user_id),
            ctx.categories.get(v.id),
            v.id in ctx.liked,
            followed_by_me=v.user_id in ctx.followed_authors,
            author_follower_count=ctx.author_follower_counts.get(v.user_id, 0),
            viewer_id=viewer_id,
        )
        for v in videos
    ]


async def list_categories(session: AsyncSession, *, include_inactive: bool = False) -> list[ReelCategory]:
    query = select(ReelCategory).order_by(ReelCategory.sort_order, ReelCategory.name)
    if not include_inactive:
        query = query.where(ReelCategory.is_active.is_(True))
    return list((await session.execute(query)).scalars().all())


# --- Likes ------------------------------------------------------------------


async def like(session: AsyncSession, video: Video, user_id: uuid.UUID) -> int:
    """Idempotent: liking twice is a no-op. Returns the new like count."""
    inserted = await session.scalar(
        insert(VideoLike)
        .values(video_id=video.id, user_id=user_id)
        .on_conflict_do_nothing()
        .returning(VideoLike.video_id)
    )
    if inserted is not None:
        count = await session.scalar(
            update(Video).where(Video.id == video.id).values(like_count=Video.like_count + 1).returning(Video.like_count)
        )
    else:
        count = video.like_count
    await session.commit()
    return count


async def unlike(session: AsyncSession, video: Video, user_id: uuid.UUID) -> int:
    removed = await session.scalar(
        delete(VideoLike)
        .where(VideoLike.video_id == video.id, VideoLike.user_id == user_id)
        .returning(VideoLike.video_id)
    )
    if removed is not None:
        count = await session.scalar(
            update(Video)
            .where(Video.id == video.id)
            .values(like_count=func.greatest(Video.like_count - 1, 0))
            .returning(Video.like_count)
        )
    else:
        count = video.like_count
    await session.commit()
    return count


# --- Comments -----------------------------------------------------------------


async def add_comment(session: AsyncSession, video: Video, user_id: uuid.UUID, body: str) -> tuple[VideoComment, int]:
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment cannot be empty")
    comment = VideoComment(video_id=video.id, user_id=user_id, body=body)
    session.add(comment)
    await session.flush()
    count = await session.scalar(
        update(Video).where(Video.id == video.id).values(comment_count=Video.comment_count + 1).returning(Video.comment_count)
    )
    await session.commit()
    await session.refresh(comment)
    return comment, count


async def list_comments(
    session: AsyncSession, video_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[tuple[VideoComment, User | None]], int]:
    total = await session.scalar(select(func.count()).where(VideoComment.video_id == video_id))
    result = await session.execute(
        select(VideoComment, User)
        .outerjoin(User, User.id == VideoComment.user_id)
        .where(VideoComment.video_id == video_id)
        .order_by(VideoComment.created_at.desc(), VideoComment.id)
        .limit(limit)
        .offset(offset)
    )
    return [(c, u) for c, u in result.all()], total or 0


async def delete_comment(session: AsyncSession, video: Video, comment_id: uuid.UUID, user_id: uuid.UUID) -> int:
    """Allowed for the commenter or the reel's owner."""
    comment = await session.get(VideoComment, comment_id)
    if comment is None or comment.video_id != video.id:
        raise HTTPException(status_code=404, detail="Comment not found")
    if user_id not in (comment.user_id, video.user_id):
        raise HTTPException(status_code=403, detail="Not authorized to delete this comment")

    removed = await session.scalar(
        delete(VideoComment).where(VideoComment.id == comment_id).returning(VideoComment.id)
    )
    if removed is not None:
        count = await session.scalar(
            update(Video)
            .where(Video.id == video.id)
            .values(comment_count=func.greatest(Video.comment_count - 1, 0))
            .returning(Video.comment_count)
        )
    else:
        count = video.comment_count
    await session.commit()
    return count


# --- Shares -------------------------------------------------------------------


async def record_share(session: AsyncSession, video: Video, user_id: uuid.UUID, channel: str | None) -> int:
    session.add(VideoShare(video_id=video.id, user_id=user_id, channel=channel))
    count = await session.scalar(
        update(Video).where(Video.id == video.id).values(share_count=Video.share_count + 1).returning(Video.share_count)
    )
    await session.commit()
    return count


# --- Account deletion -----------------------------------------------------------


async def release_user_interactions(session: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Called before a user row is deleted: their likes/comments go away via
    ON DELETE CASCADE, but the counters on *other people's* reels wouldn't
    follow on their own. Shares are a historical event count and are left as-is.
    Caller commits."""
    like_rows = await session.execute(
        delete(VideoLike).where(VideoLike.user_id == user_id).returning(VideoLike.video_id)
    )
    liked_ids = list(like_rows.scalars().all())
    if liked_ids:
        await session.execute(
            update(Video)
            .where(Video.id.in_(liked_ids))
            .values(like_count=func.greatest(Video.like_count - 1, 0))
        )

    comment_rows = await session.execute(
        delete(VideoComment).where(VideoComment.user_id == user_id).returning(VideoComment.video_id)
    )
    per_video: dict[uuid.UUID, int] = {}
    for video_id in comment_rows.scalars().all():
        per_video[video_id] = per_video.get(video_id, 0) + 1
    for video_id, n in per_video.items():
        await session.execute(
            update(Video).where(Video.id == video_id).values(comment_count=func.greatest(Video.comment_count - n, 0))
        )

    return {"reelLikes": len(liked_ids), "reelComments": sum(per_video.values())}
