"""A creator's reels with aggregate stats, for profile screens.

Both queries are served by idx_videos_user_status_created
(user_id, status, created_at), so they stay cheap per user regardless of how
large the videos table grows. If a creator ever has so many reels that the
SUM becomes noticeable, replace `get_summary` with a read from a maintained
`creator_stats` table — nothing outside this module needs to change.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import Video


@dataclass
class CreatorSummary:
    total_reels: int
    total_likes: int
    total_views: int


async def get_summary(session: AsyncSession, user_id: uuid.UUID) -> CreatorSummary:
    row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(Video.like_count), 0),
                func.coalesce(func.sum(Video.view_count), 0),
            ).where(Video.user_id == user_id, Video.status == "ready")
        )
    ).one()
    return CreatorSummary(total_reels=row[0], total_likes=int(row[1]), total_views=int(row[2]))


async def list_reels(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int, offset: int
) -> list[Video]:
    result = await session.execute(
        select(Video)
        .where(Video.user_id == user_id, Video.status == "ready")
        .order_by(Video.created_at.desc(), Video.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())
