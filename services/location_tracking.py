"""Location sharing for 48 hours after a truck post.

Not live tracking. When a user posts a truck (or reactivates an expired post) a window opens:
for LOCATION_TRACKING_HOURS the app reports where the phone is every ~30 minutes (a background
job on Android; on app open elsewhere), so people who want to request a service can find them.
Each report moves the pin of the user's live post and is kept as their last known position.

The window ends by itself, and earlier when the user logs out or deletes the post that started
it. Ending it also forgets the last position. The app is told (`active: false`) as soon as it
reports into a window that is over, so it can stop its background job.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    LOCATION_REPORT_INTERVAL_MINUTES,
    LOCATION_REPORT_MIN_SECONDS,
    LOCATION_TRACKING_HOURS,
    logger,
)
from db.base import TruckRoute, UserLocationTracking
from services.post_expiry import ACTIVE_POST_STATUSES, get_active_post_for_user

# Why a window ended, kept on the row.
EXPIRED, LOGOUT, POST_DELETED, USER_STOPPED = "expired", "logout", "post_deleted", "user_stopped"


def _now() -> datetime:
    return datetime.utcnow()


def _is_on(row: UserLocationTracking | None, now: datetime) -> bool:
    return row is not None and row.stopped_at is None and now < row.ends_at


def _status(row: UserLocationTracking | None, now: datetime) -> dict:
    on = _is_on(row, now)
    return {
        "active": on,
        "startedAt": row.started_at.isoformat() if on else None,
        "endsAt": row.ends_at.isoformat() if on else None,
        "lastReportedAt": row.last_reported_at.isoformat() if on and row.last_reported_at else None,
        "intervalMinutes": LOCATION_REPORT_INTERVAL_MINUTES,
        "hours": LOCATION_TRACKING_HOURS,
    }


async def _locked(session: AsyncSession, user_id: uuid.UUID) -> UserLocationTracking | None:
    result = await session.execute(
        select(UserLocationTracking).where(UserLocationTracking.user_id == user_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def start_tracking(session: AsyncSession, user_id: uuid.UUID, post_id: uuid.UUID | None) -> dict:
    """Open (or restart) the window: a new post, or a reactivated one. Commits."""
    now = _now()
    row = await _locked(session, user_id)
    if row is None:
        row = UserLocationTracking(user_id=user_id, started_at=now, ends_at=now)
        session.add(row)
    row.post_id = post_id
    row.started_at = now
    row.ends_at = now + timedelta(hours=LOCATION_TRACKING_HOURS)
    row.stopped_at = None
    row.stop_reason = None
    status = _status(row, now)  # read before the commit, which may expire the row
    await session.commit()
    return status


async def start_tracking_quietly(session: AsyncSession, user_id: uuid.UUID, post_id: uuid.UUID | None) -> None:
    """For the post endpoints: sharing must never be the reason a post fails."""
    try:
        await start_tracking(session, user_id, post_id)
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        logger.error("Could not start location sharing for user %s: %s", user_id, e)


async def get_status(session: AsyncSession, user_id: uuid.UUID) -> dict:
    row = await session.get(UserLocationTracking, user_id)
    return _status(row, _now())


async def stop_tracking(session: AsyncSession, user_id: uuid.UUID, reason: str) -> dict:
    """End the window now and forget the last position. Safe when there is none. Commits."""
    now = _now()
    row = await _locked(session, user_id)
    if row is not None and row.stopped_at is None:
        _finish(row, now, reason)
    status = _status(row, now)
    await session.commit()
    return status


def _finish(row: UserLocationTracking, now: datetime, reason: str) -> None:
    row.stopped_at = now
    row.stop_reason = reason
    row.last_lat = row.last_lng = row.last_accuracy_m = None


async def on_post_deleted(session: AsyncSession, user_id: uuid.UUID, post_id: uuid.UUID) -> None:
    """The post that opened the window is gone: stop, unless another post is still live (one person
    can't have two, but a reactivated old post could be). Deleting some older post doesn't stop it."""
    try:
        row = await session.get(UserLocationTracking, user_id)
        if row is None or row.post_id != post_id:
            return
        if await get_active_post_for_user(session, user_id) is None:
            await stop_tracking(session, user_id, POST_DELETED)
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        logger.error("Could not stop location sharing after a post was deleted (user %s): %s", user_id, e)


async def record_location(
    session: AsyncSession,
    user_id: uuid.UUID,
    lat: float,
    lng: float,
    accuracy: float | None = None,
    place: dict | None = None,
) -> dict:
    """One report from the phone. Outside a window nothing is stored and `active` is false (the app
    then stops its job). A report right after the previous one is acknowledged but not stored."""
    now = _now()
    row = await _locked(session, user_id)
    if not _is_on(row, now):
        if row is not None and row.stopped_at is None:  # ran out, and nobody swept it yet
            _finish(row, now, EXPIRED)
            result = {**_status(row, now), "recorded": False}
            await session.commit()
        else:
            result = {**_status(row, now), "recorded": False}
            await session.rollback()
        return result

    if row.last_reported_at and (now - row.last_reported_at).total_seconds() < LOCATION_REPORT_MIN_SECONDS:
        result = {**_status(row, now), "recorded": False}
        await session.rollback()
        return result

    row.last_lat, row.last_lng, row.last_accuracy_m = lat, lng, accuracy
    row.last_reported_at = now
    row.report_count += 1
    await _move_live_posts(session, user_id, lat, lng, place or {}, now)
    result = {**_status(row, now), "recorded": True}
    await session.commit()
    return result


async def _move_live_posts(
    session: AsyncSession, user_id: uuid.UUID, lat: float, lng: float, place: dict, now: datetime
) -> None:
    """The pin of the user's live post is where the truck is now (heatmap, search, 1:1 requests all
    read `current_location`). Other fields (the place name ...) are kept unless the phone sent new ones."""
    posts = (
        await session.execute(
            select(TruckRoute).where(
                TruckRoute.user_id == user_id,
                TruckRoute.status.in_(ACTIVE_POST_STATUSES),
                TruckRoute.expires_at > now,
            )
        )
    ).scalars()
    for post in posts:
        location = dict(post.current_location or {})
        location["lat"], location["lng"] = lat, lng
        for key in ("name", "city", "state", "pincode"):
            if place.get(key):
                location[key] = place[key]
        post.current_location = location  # a new dict, so the JSONB change is noticed


async def finish_expired(session: AsyncSession) -> int:
    """Background sweep: windows that ran out forget their last position even if the user never
    reports again. Commits."""
    now = _now()
    result = await session.execute(
        update(UserLocationTracking)
        .where(UserLocationTracking.stopped_at.is_(None), UserLocationTracking.ends_at <= now)
        .values(stopped_at=now, stop_reason=EXPIRED, last_lat=None, last_lng=None, last_accuracy_m=None)
    )
    await session.commit()
    return result.rowcount or 0
