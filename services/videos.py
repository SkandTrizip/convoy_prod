"""Video reels: direct-to-Bunny upload, encoding-status sync, and feed reads.

Upload flow (no byte-proxying): `create_upload` asks Bunny for a video
placeholder and returns TUS credentials the client uploads to directly. Once
Bunny finishes encoding it calls our webhook (routers/webhooks.py); locally,
where Bunny can't reach the server, `sync_status` can be triggered manually
instead (routers/videos.py's /sync-status endpoint) — both paths run the same
logic here.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import Text, cast, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import HEATMAP_REEL_WINDOW_DAYS, MAX_VIDEO_DURATION_SECONDS
from db.base import ReelCategory, Video, VideoCategory
from services import bunny_stream, reel_area, reel_ranking, reel_social


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


async def _feed_conditions(
    session: AsyncSession,
    *,
    category: list[str] | None,
    hashtag: str | None,
    exclude_user_id: uuid.UUID | None,
) -> list:
    """WHERE conditions shared by every feed read: ready reels, not the viewer's own,
    optionally narrowed by category and/or hashtag."""
    conditions = [Video.status == "ready"]
    if exclude_user_id is not None:
        # A viewer's own uploads never appear in their watch feed.
        conditions.append(Video.user_id != exclude_user_id)
    if category:
        category_ids = await resolve_filter_categories(session, category)
        if category_ids:
            conditions.append(
                Video.id.in_(select(VideoCategory.video_id).where(VideoCategory.category_id.in_(category_ids)))
            )
    if hashtag:
        tag = hashtag.strip().lstrip("#").lower()
        conditions.append(Video.hashtags.contains([tag]))
    return conditions


async def list_feed(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    category: list[str] | None = None,
    hashtag: str | None = None,
    exclude_user_id: uuid.UUID | None = None,
    sort: reel_ranking.ReelSort = "latest",
) -> tuple[list[Video], int]:
    """`category`: category ids and/or slugs (a reel matching ANY of them is
    included); `hashtag` is matched after the same normalization applied on
    upload (case-insensitive, leading '#' ignored). `sort` is decided in
    services/reel_ranking.py."""
    conditions = await _feed_conditions(
        session, category=category, hashtag=hashtag, exclude_user_id=exclude_user_id
    )
    base = select(Video).where(*conditions)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    ordering = reel_ranking.order_by(sort, datetime.utcnow())
    result = await session.execute(base.order_by(*ordering).limit(limit).offset(offset))
    return list(result.scalars().all()), total or 0


async def list_area_feed(
    session: AsyncSession,
    *,
    h3_cell: str,
    limit: int,
    offset: int,
    category: list[str] | None = None,
    hashtag: str | None = None,
    exclude_user_id: uuid.UUID | None = None,
    sort: reel_ranking.ReelSort = "popular",
) -> tuple[list[Video], dict]:
    """The reels of one heatmap cell, ranked by `sort`, plus an `area` summary.

    Same reels the Reels heatmap counted for that cell: ready, posted with a
    location, inside the heatmap's recency window, exactly in the H3 cell. (The
    viewer's own reels are left out, like the main feed; the heat does count them.)
    Raises ValueError (client-safe message) for a cell we won't serve.
    """
    cell, resolution = reel_area.parse_area_cell(h3_cell)
    center_lat, center_lng, radius_m = reel_area.cell_geometry(cell)
    now = datetime.utcnow()

    conditions = await _feed_conditions(
        session, category=category, hashtag=hashtag, exclude_user_id=exclude_user_id
    )
    center = cast(literal_column(f"'SRID=4326;POINT({center_lng:.7f} {center_lat:.7f})'"), Text)
    conditions += [
        Video.location.isnot(None),
        Video.created_at >= now - timedelta(days=HEATMAP_REEL_WINDOW_DAYS),
        func.ST_DWithin(Video.location, func.ST_GeogFromText(center), radius_m),
    ]
    candidates_query = (
        select(
            Video.id,
            literal_column("ST_Y(videos.location::geometry)").label("lat"),
            literal_column("ST_X(videos.location::geometry)").label("lng"),
            Video.location_city,
            Video.location_state,
        )
        .where(*conditions)
        .order_by(*reel_ranking.order_by(sort, now))
        .limit(reel_area.MAX_CANDIDATES)
    )
    rows = (await session.execute(candidates_query)).all()

    # Exact H3 membership, keeping the ranked order.
    matched = [r for r in rows if reel_area.in_cell(float(r.lat), float(r.lng), cell, resolution)]
    label = reel_area.area_label((r.location_city, r.location_state) for r in matched)
    info = reel_area.area_info(
        cell, resolution, label, total=len(matched), capped=len(rows) >= reel_area.MAX_CANDIDATES
    )

    page_ids = [r.id for r in matched[offset : offset + limit]]
    if not page_ids:
        return [], info
    videos = {
        v.id: v for v in (await session.execute(select(Video).where(Video.id.in_(page_ids)))).scalars().all()
    }
    return [videos[i] for i in page_ids if i in videos], info


async def list_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[Video], int]:
    base = select(Video).where(Video.user_id == user_id)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    result = await session.execute(base.order_by(Video.created_at.desc()).limit(limit).offset(offset))
    return list(result.scalars().all()), total or 0


async def delete_own_video(session: AsyncSession, video: Video, user_id: uuid.UUID) -> None:
    """Hard-delete a reel the caller owns, in any status. Bunny goes first so a
    Bunny outage leaves the row (and the user's retry) intact instead of orphaning
    a stream; Bunny's 404 is ignored, so a retry after a half-finished delete
    works. Likes, comments, shares and category tags go with the row (ON DELETE
    CASCADE)."""
    if video.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this reel")
    await bunny_stream.delete_video(video.bunny_video_id)
    await session.delete(video)
    await session.commit()


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
