"""Video reels: direct-to-Bunny upload, encoding-status sync, and feed reads.

Upload flow (no byte-proxying): `create_upload` asks Bunny for a video
placeholder and returns TUS credentials the client uploads to directly. Once
Bunny finishes encoding it calls our webhook (routers/webhooks.py); locally,
where Bunny can't reach the server, `sync_status` can be triggered manually
instead (routers/videos.py's /sync-status endpoint) — both paths run the same
logic here.
"""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import MAX_VIDEO_DURATION_SECONDS
from db.base import ReelCategory, Video, VideoCategory
from services import bunny_stream, reel_social


async def create_upload(
    session: AsyncSession,
    user_id: uuid.UUID,
    title: str,
    description: str | None,
    *,
    category_ids: list[str],
    hashtags: list[str] | None,
    latitude: float | None,
    longitude: float | None,
) -> dict:
    # Validate everything client-supplied before creating the Bunny placeholder,
    # so a bad request never leaves an orphaned video on Bunny's side.
    categories = await reel_social.resolve_categories(session, category_ids)
    normalized_tags = reel_social.normalize_hashtags(hashtags)

    bunny_video_id = await bunny_stream.create_video(title)

    video = Video(
        user_id=user_id,
        bunny_video_id=bunny_video_id,
        title=title,
        description=description,
        hashtags=normalized_tags,
        status="pending_upload",
    )
    session.add(video)
    await session.flush()
    if latitude is not None and longitude is not None:
        await reel_social.apply_location(session, video, latitude, longitude)
    reel_social.set_video_categories(session, video.id, categories)
    await session.commit()
    await session.refresh(video)

    tus_credentials = bunny_stream.build_tus_upload_credentials(bunny_video_id)

    return {
        "video": video,
        "tusUploadUrl": tus_credentials.upload_url,
        "tusHeaders": tus_credentials.headers,
        "expiresAt": datetime.fromtimestamp(tus_credentials.expires_at, tz=timezone.utc),
    }


async def get_video(session: AsyncSession, video_id: uuid.UUID) -> Video:
    video = await session.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


async def get_by_bunny_video_id(session: AsyncSession, bunny_video_id: str) -> Video:
    result = await session.execute(select(Video).where(Video.bunny_video_id == bunny_video_id))
    video = result.scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


async def resolve_filter_categories(session: AsyncSession, values: list[str]) -> list[uuid.UUID]:
    """Feed filter values -> category ids. Each value may be a category id or
    slug, repeated params and comma-separated lists both work. Deactivated
    categories still filter (existing reels keep them); an unknown value is a
    400 rather than a silently empty feed."""
    tokens = [t.strip() for v in values for t in v.split(",") if t.strip()]
    if not tokens:
        return []
    ids: set[uuid.UUID] = set()
    slugs: set[str] = set()
    for token in tokens:
        try:
            ids.add(uuid.UUID(token))
        except ValueError:
            slugs.add(token.lower())

    conditions = []
    if ids:
        conditions.append(ReelCategory.id.in_(ids))
    if slugs:
        conditions.append(ReelCategory.slug.in_(slugs))
    rows = (await session.execute(select(ReelCategory).where(or_(*conditions)))).scalars().all()

    found = {str(c.id) for c in rows} | {c.slug for c in rows}
    unknown = [t for t in tokens if t.lower() not in found]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown category: {', '.join(unknown)}")
    return [c.id for c in rows]


async def list_feed(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    category: list[str] | None = None,
    hashtag: str | None = None,
    exclude_user_id: uuid.UUID | None = None,
) -> tuple[list[Video], int]:
    """`category`: category ids and/or slugs (a reel matching ANY of them is
    included); `hashtag` is matched after the same normalization applied on
    upload (case-insensitive, leading '#' ignored)."""
    base = select(Video).where(Video.status == "ready")
    if exclude_user_id is not None:
        # A viewer's own uploads never appear in their watch feed.
        base = base.where(Video.user_id != exclude_user_id)
    if category:
        category_ids = await resolve_filter_categories(session, category)
        if category_ids:
            base = base.where(
                Video.id.in_(select(VideoCategory.video_id).where(VideoCategory.category_id.in_(category_ids)))
            )
    if hashtag:
        tag = hashtag.strip().lstrip("#").lower()
        base = base.where(Video.hashtags.contains([tag]))
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    result = await session.execute(base.order_by(Video.created_at.desc()).limit(limit).offset(offset))
    return list(result.scalars().all()), total or 0


async def list_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[Video], int]:
    base = select(Video).where(Video.user_id == user_id)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    result = await session.execute(base.order_by(Video.created_at.desc()).limit(limit).offset(offset))
    return list(result.scalars().all()), total or 0


async def sync_status(session: AsyncSession, video: Video) -> Video:
    info = await bunny_stream.get_video(video.bunny_video_id)

    if info.status in bunny_stream.STATUS_FAILED_CODES:
        video.status = "failed"
        video.rejection_reason = f"Bunny reported encoding status {info.status}"
    elif info.status == bunny_stream.STATUS_FINISHED:
        if info.length_seconds > MAX_VIDEO_DURATION_SECONDS:
            await bunny_stream.delete_video(video.bunny_video_id)
            video.status = "rejected"
            video.duration_seconds = info.length_seconds
            video.rejection_reason = (
                f"Video is {info.length_seconds}s, exceeds the {MAX_VIDEO_DURATION_SECONDS}s limit"
            )
        else:
            video.status = "ready"
            video.duration_seconds = info.length_seconds
            video.playback_url = bunny_stream.build_playback_url(video.bunny_video_id)
            video.thumbnail_url = bunny_stream.build_thumbnail_url(
                video.bunny_video_id, info.thumbnail_file_name
            )
    # Any other status (Created/Uploaded/Processing/Transcoding) means encoding
    # is still in progress - nothing to update yet, caller can retry later.

    video.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(video)
    return video
