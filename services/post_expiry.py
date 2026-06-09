import asyncio
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from config import POST_EXPIRE_CHECK_INTERVAL_SECONDS, POST_EXPIRE_HOURS, logger
from db.base import TruckRoute

ACTIVE_POST_STATUSES = ("available", "active")
EXPIRED_STATUS = "expired"
DEFAULT_ACTIVE_STATUS = "active"


def post_expires_at(from_time: datetime | None = None) -> datetime:
    """Return expiry timestamp for a new or reactivated post."""
    base = from_time or datetime.utcnow()
    return base + timedelta(hours=POST_EXPIRE_HOURS)


def is_post_expired(route: TruckRoute, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    return route.status == EXPIRED_STATUS or route.expires_at <= now


def apply_post_reactivation(route: TruckRoute, now: datetime | None = None) -> None:
    """Extend an expired post for another POST_EXPIRE_HOURS window."""
    now = now or datetime.utcnow()
    route.status = DEFAULT_ACTIVE_STATUS
    route.created_at = now
    route.expires_at = post_expires_at(now)


async def expire_overdue_posts(session: AsyncSession) -> int:
    """Mark active posts past expires_at as expired."""
    now = datetime.utcnow()
    result = await session.execute(
        update(TruckRoute)
        .where(
            TruckRoute.status.in_(ACTIVE_POST_STATUSES),
            TruckRoute.expires_at <= now,
        )
        .values(status=EXPIRED_STATUS)
    )
    await session.commit()
    return result.rowcount or 0


async def run_post_expiry_loop() -> None:
    """Background job: expire truck posts every few minutes."""
    from db import async_session

    while True:
        try:
            async with async_session() as session:
                count = await expire_overdue_posts(session)
                if count:
                    logger.info("Auto-expired %s truck post(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Post expiry job failed: %s", e, exc_info=True)
        await asyncio.sleep(POST_EXPIRE_CHECK_INTERVAL_SECONDS)
