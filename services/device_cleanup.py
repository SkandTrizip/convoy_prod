import asyncio
from datetime import datetime, timedelta

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    DEVICE_CLEANUP_CHECK_INTERVAL_SECONDS,
    DEVICE_DEACTIVATE_AFTER_DAYS,
    DEVICE_DELETE_AFTER_INACTIVE_DAYS,
    logger,
)
from db.base import UserDevice


async def cleanup_stale_devices(session: AsyncSession) -> tuple[int, int]:
    """Two-tier retention, judged on last_app_seen (reflects real usage —
    see db/base.py's UserDevice docstring, not last_token_sync):
    1. Active devices gone quiet for DEVICE_DEACTIVATE_AFTER_DAYS get
       soft-deactivated.
    2. Already-inactive devices (from step 1, or from an FCM
       UNREGISTERED/INVALID_ARGUMENT deactivation) get hard-deleted once
       they've *also* been quiet for a further DEVICE_DELETE_AFTER_INACTIVE_DAYS.
    Returns (deactivated_count, deleted_count)."""
    now = datetime.utcnow()

    deactivate_result = await session.execute(
        update(UserDevice)
        .where(
            UserDevice.is_active.is_(True),
            UserDevice.last_app_seen < now - timedelta(days=DEVICE_DEACTIVATE_AFTER_DAYS),
        )
        .values(is_active=False)
    )
    delete_result = await session.execute(
        delete(UserDevice).where(
            UserDevice.is_active.is_(False),
            UserDevice.last_app_seen < now - timedelta(days=DEVICE_DELETE_AFTER_INACTIVE_DAYS),
        )
    )
    await session.commit()
    return deactivate_result.rowcount or 0, delete_result.rowcount or 0


async def run_device_cleanup_loop() -> None:
    """Background job: prune stale push-device registrations once a day."""
    from db import async_session

    while True:
        try:
            async with async_session() as session:
                deactivated, deleted = await cleanup_stale_devices(session)
                if deactivated or deleted:
                    logger.info(
                        "Device cleanup: deactivated %s, deleted %s", deactivated, deleted
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Device cleanup job failed: %s", e, exc_info=True)
        await asyncio.sleep(DEVICE_CLEANUP_CHECK_INTERVAL_SECONDS)
