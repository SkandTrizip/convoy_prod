"""Per-device push-registration lookups, used by the campaign engine, the
ad-hoc admin send API, the single-event send path, and the device
register/logout endpoints. Replaces the old single-`push_token`-column
lookups now that a user can have many UserDevice rows."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import UserDevice


@dataclass
class DeviceToken:
    device_id: str
    fcm_token: str


async def upsert_device(
    session: AsyncSession,
    user_id: uuid.UUID,
    device_id: str,
    platform: str,
    fcm_token: str,
    app_version: Optional[str] = None,
    device_name: Optional[str] = None,
) -> UserDevice:
    """Idempotent device sync. Caller commits.

    device_id and fcm_token are both globally unique. If this fcm_token
    already belongs to a *different* device_id, that row is stale (the
    token migrated — reinstall, token rotation onto a row we don't know
    about yet) and is deleted outright so the unique constraint doesn't
    reject this upsert.
    """
    now = datetime.utcnow()

    stale_owner = await session.execute(
        select(UserDevice).where(
            UserDevice.fcm_token == fcm_token, UserDevice.device_id != device_id
        )
    )
    stale_row = stale_owner.scalar_one_or_none()
    if stale_row is not None:
        await session.delete(stale_row)
        await session.flush()

    result = await session.execute(select(UserDevice).where(UserDevice.device_id == device_id))
    device = result.scalar_one_or_none()

    if device is None:
        device = UserDevice(
            user_id=user_id,
            device_id=device_id,
            platform=platform,
            fcm_token=fcm_token,
            device_name=device_name,
            app_version=app_version,
        )
        session.add(device)
    else:
        # Reassign user_id too — a device_id resyncing under a different
        # user is a shared device or a login-as-someone-else, not an error.
        device.user_id = user_id
        device.platform = platform
        device.fcm_token = fcm_token
        device.device_name = device_name
        device.app_version = app_version

    # A sync call is itself proof the app was just open, so it counts as an
    # app-seen event too — this matters for the legacy shim (routers/users.py),
    # which never gets the opportunistic middleware update since old app
    # versions don't send the X-Device-Id header.
    device.is_active = True
    device.last_token_sync = now
    device.last_app_seen = now

    await session.flush()
    return device


async def touch_last_seen(
    session: AsyncSession,
    device_id: Optional[str],
    *,
    min_interval: timedelta = timedelta(hours=1),
) -> None:
    """Opportunistic, best-effort: called from middleware on every
    authenticated request that carries an X-Device-Id header. No-ops
    silently if device_id is absent or doesn't match any row (an
    unregistered/typo'd device id must never fail a request), and only
    writes if last_app_seen is stale by more than min_interval, so a chatty
    app doesn't issue a write on every single request. Commits on its own —
    this is a fire-and-forget side effect the caller has no other chance to
    commit."""
    if not device_id:
        return

    threshold = datetime.utcnow() - min_interval
    result = await session.execute(
        update(UserDevice)
        .where(UserDevice.device_id == device_id, UserDevice.last_app_seen < threshold)
        .values(last_app_seen=datetime.utcnow())
    )
    if result.rowcount:
        await session.commit()


async def get_active_devices_for_user_ids(
    session: AsyncSession, user_ids: List[uuid.UUID]
) -> Dict[str, List[DeviceToken]]:
    """user_id (str) -> active devices, for fanning out one send per device."""
    if not user_ids:
        return {}

    result = await session.execute(
        select(UserDevice.user_id, UserDevice.device_id, UserDevice.fcm_token).where(
            UserDevice.user_id.in_(user_ids), UserDevice.is_active.is_(True)
        )
    )
    devices: Dict[str, List[DeviceToken]] = {}
    for user_id, device_id, fcm_token in result.all():
        devices.setdefault(str(user_id), []).append(DeviceToken(device_id, fcm_token))
    return devices


async def deactivate_devices(session: AsyncSession, device_ids: List[str]) -> None:
    """Bulk soft-deactivate — used by the FCM UNREGISTERED/INVALID_ARGUMENT
    path and the stale-device cleanup job. Caller commits."""
    if not device_ids:
        return
    await session.execute(
        update(UserDevice).where(UserDevice.device_id.in_(device_ids)).values(is_active=False)
    )


async def logout_device(session: AsyncSession, user_id: uuid.UUID, device_id: str) -> bool:
    """Deregister push for one of a user's devices. Returns False if no
    matching active row for that user (caller should 404). Caller commits."""
    result = await session.execute(
        update(UserDevice)
        .where(
            UserDevice.device_id == device_id,
            UserDevice.user_id == user_id,
            UserDevice.is_active.is_(True),
        )
        .values(is_active=False)
    )
    return bool(result.rowcount)
