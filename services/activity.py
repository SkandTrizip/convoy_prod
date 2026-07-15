import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from db.base import TruckRoute, UserActivity

RECENT_LIMIT = 10


async def _trim_to_last_n(session: AsyncSession, user_id: uuid.UUID, activity_type: str, n: int) -> None:
    keep_ids = (
        select(UserActivity.id)
        .where(UserActivity.user_id == user_id, UserActivity.type == activity_type)
        .order_by(UserActivity.created_at.desc())
        .limit(n)
    )
    await session.execute(
        delete(UserActivity).where(
            UserActivity.user_id == user_id,
            UserActivity.type == activity_type,
            UserActivity.id.notin_(keep_ids),
        )
    )


async def record_post_activity(
    session: AsyncSession, user_id: uuid.UUID, truck_route_id: uuid.UUID
) -> None:
    """Best-effort: never raises, so a logging failure can't break post creation."""
    try:
        session.add(UserActivity(user_id=user_id, type="post", truck_route_id=truck_route_id))
        await _trim_to_last_n(session, user_id, "post", RECENT_LIMIT)
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.error("Error recording post activity: %s", e, exc_info=True)


async def record_search_activity(
    session: AsyncSession, user_id: uuid.UUID, search_criteria: dict[str, Any]
) -> None:
    """Best-effort: never raises, so a logging failure can't break search results.

    Dedupes against the user's immediately preceding search entry — an identical repeat
    search (e.g. a refresh) just bumps that entry's timestamp instead of adding a row.
    """
    try:
        result = await session.execute(
            select(UserActivity)
            .where(UserActivity.user_id == user_id, UserActivity.type == "search")
            .order_by(UserActivity.created_at.desc())
            .limit(1)
        )
        last = result.scalar_one_or_none()
        if last is not None and last.search_criteria == search_criteria:
            last.created_at = datetime.utcnow()
        else:
            session.add(
                UserActivity(user_id=user_id, type="search", search_criteria=search_criteria)
            )
        await _trim_to_last_n(session, user_id, "search", RECENT_LIMIT)
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.error("Error recording search activity: %s", e, exc_info=True)


async def get_recent_posts(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    from db.serializers import truck_route_to_dict
    from services.destinations import get_destinations_for_routes

    result = await session.execute(
        select(TruckRoute, UserActivity.created_at.label("activity_at"))
        .join(UserActivity, UserActivity.truck_route_id == TruckRoute.id)
        .where(UserActivity.user_id == user_id, UserActivity.type == "post")
        .order_by(UserActivity.created_at.desc())
        .limit(RECENT_LIMIT)
    )
    rows = result.all()

    destinations_by_route = await get_destinations_for_routes(
        session, [route.id for route, _ in rows]
    )

    items = []
    for route, activity_at in rows:
        item = truck_route_to_dict(route, destinations_by_route.get(str(route.id), []))
        item["activityAt"] = activity_at
        items.append(item)
    return items


async def get_recent_searches(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    result = await session.execute(
        select(UserActivity)
        .where(UserActivity.user_id == user_id, UserActivity.type == "search")
        .order_by(UserActivity.created_at.desc())
        .limit(RECENT_LIMIT)
    )
    rows = result.scalars().all()
    return [{**row.search_criteria, "searchedAt": row.created_at} for row in rows]
