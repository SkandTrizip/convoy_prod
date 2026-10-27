"""1:1 video calls: one host (camera+mic) and at most one guest (mic-only).

`_reap_overdue` runs at the top of every read/join path rather than via a
background scheduler — the trade-off is a call can show `live` for a few
seconds past its cutoff if nothing queries it, but no client can ever get a
token that outlives the cutoff regardless (enforced by Agora token expiry
itself, see services/agora_client.py).
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import OneToOneCall
from services import agora_client


async def _reap_overdue(session: AsyncSession) -> None:
    cutoff = datetime.utcnow() - timedelta(seconds=agora_client.max_call_seconds())
    now = datetime.utcnow()
    await session.execute(
        update(OneToOneCall)
        .where(OneToOneCall.status == "live", OneToOneCall.started_at <= cutoff)
        .values(status="ended", ended_at=now)
    )
    await session.commit()


def _not_after_ts(started_at: datetime) -> int:
    return int(started_at.replace(tzinfo=timezone.utc).timestamp()) + agora_client.max_call_seconds()


async def _get_or_raise(session: AsyncSession, call_id: uuid.UUID) -> OneToOneCall:
    call = await session.get(OneToOneCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


def _session_credentials(
    call_id: uuid.UUID, channel_name: str, user_id: uuid.UUID, started_at: datetime
) -> dict:
    uid = agora_client.uid_for_user(user_id)
    not_after_ts = _not_after_ts(started_at)
    rtc_token, expire_ts = agora_client.build_rtc_token(
        channel_name=channel_name, uid=uid, is_host=True, not_after_ts=not_after_ts
    )
    rtm_token, _ = agora_client.build_rtm_token(account=str(user_id), not_after_ts=not_after_ts)
    return {
        "callId": call_id,
        "appId": agora_client.AGORA_APP_ID,
        "channelName": channel_name,
        "uid": uid,
        "rtcToken": rtc_token,
        "rtmToken": rtm_token,
        "expiresAt": datetime.fromtimestamp(expire_ts, tz=timezone.utc),
    }


async def start_call(session: AsyncSession, host_user_id: uuid.UUID, title: str, host_name: str) -> dict:
    agora_client.require_configured()  # before any DB write - never create an orphaned call row

    channel_name = f"one2one-{uuid.uuid4().hex}"
    call = OneToOneCall(
        host_user_id=host_user_id,
        channel_name=channel_name,
        title=title,
        host_name=host_name,
        status="live",
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)

    creds = _session_credentials(call.id, channel_name, host_user_id, call.started_at)
    return creds


async def join_call(session: AsyncSession, call_id: uuid.UUID, guest_user_id: uuid.UUID) -> dict:
    agora_client.require_configured()  # before claiming the guest slot - never burn it on a 503

    await _reap_overdue(session)
    call = await _get_or_raise(session, call_id)
    if call.status != "live":
        raise HTTPException(status_code=409, detail="Call has already ended")

    if guest_user_id == call.host_user_id:
        raise HTTPException(status_code=403, detail="Host cannot join their own call as a guest")

    if call.guest_user_id is not None and call.guest_user_id != guest_user_id:
        # someone else already has the slot - the room is full for this
        # second, different guest (their own rejoin is allowed, e.g. after a
        # dropped connection)
        raise HTTPException(status_code=409, detail="Call already has a guest")

    if call.guest_user_id != guest_user_id:
        # Atomically claim the slot only if it's still unclaimed. A plain
        # read-then-write (checking call.guest_user_id in Python, then
        # writing it) would let two different users who both read
        # guest_user_id=NULL before either commits both pass the check above
        # and both end up with valid Agora publisher credentials for the same
        # "1:1" call - this conditional UPDATE is the actual race guard.
        result = await session.execute(
            update(OneToOneCall)
            .where(
                OneToOneCall.id == call_id,
                OneToOneCall.status == "live",
                OneToOneCall.guest_user_id.is_(None),
            )
            .values(guest_user_id=guest_user_id, updated_at=datetime.utcnow())
        )
        await session.commit()
        if result.rowcount == 0:
            # Lost the race (or the call ended) between our read above and
            # this UPDATE - refresh and report the real reason.
            await session.refresh(call)
            if call.guest_user_id is not None and call.guest_user_id != guest_user_id:
                raise HTTPException(status_code=409, detail="Call already has a guest")
            if call.status != "live":
                raise HTTPException(status_code=409, detail="Call has already ended")
        else:
            await session.refresh(call)

    # the guest also gets publisher rights here (unlike a 1:many audience, who
    # only ever subscribe) - they need to publish their microphone. It's the
    # client's own media options that stop them publishing a camera track,
    # not this token.
    return _session_credentials(call.id, call.channel_name, guest_user_id, call.started_at)


async def end_call(session: AsyncSession, call_id: uuid.UUID) -> OneToOneCall:
    call = await _get_or_raise(session, call_id)
    if call.status == "live":
        call.status = "ended"
        call.ended_at = datetime.utcnow()
        call.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(call)
    return call


async def get_call(session: AsyncSession, call_id: uuid.UUID) -> OneToOneCall:
    await _reap_overdue(session)
    return await _get_or_raise(session, call_id)


async def list_live(session: AsyncSession, *, limit: int, offset: int) -> tuple[list[OneToOneCall], int]:
    await _reap_overdue(session)
    base = select(OneToOneCall).where(OneToOneCall.status == "live")
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    result = await session.execute(base.order_by(OneToOneCall.started_at.desc()).limit(limit).offset(offset))
    return list(result.scalars().all()), total or 0
