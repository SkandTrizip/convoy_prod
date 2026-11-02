"""Paid 1:1 call requests: request -> provider accepts -> wallet hold -> call -> settle.

    pending -> accepted -> ringing -> live -> awaiting_feedback -> completed
    exits:  rejected (provider) | cancelled (requester) | expired (30 min window)
            | failed (call ended before connecting for the threshold)

Money rules (all amounts in INR, wallet ledger entries reference the request):
  * At request time the requester must be able to cover the WHOLE offer
    (`available - call_commitments >= amount`), but only `hold_amount` is frozen:
    max(25% of the offer, Rs25), capped at the offer. That is the "call amount":
    the cost of connecting, owed to the provider once the call has connected for
    CALL_CONNECT_THRESHOLD_SECONDS, whatever the requester thinks of it.
  * Every exit except `completed` releases the hold in full.
  * After a call that connected the requester is asked "were you satisfied?":
    yes -> pays the full offer, no -> pays only the call amount. No answer within
    CALL_FEEDBACK_TTL_HOURS counts as "no".

Every function here that changes state takes a row lock on the request, then
re-checks its status, so two taps / a tap racing the sweeper can't both act.
Functions commit; the wallet helpers they call do not (see services/wallet.py).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    CALL_CONNECT_THRESHOLD_SECONDS,
    CALL_DURATION_OPTIONS_MINUTES,
    CALL_DEVICE_TAKEOVER_SECONDS,
    CALL_END_GRACE_SECONDS,
    CALL_FEEDBACK_TTL_HOURS,
    CALL_HEARTBEAT_GRACE_SECONDS,
    CALL_HOLD_FRACTION,
    CALL_HOLD_MIN_INR,
    CALL_MAX_AMOUNT_INR,
    CALL_MIN_AMOUNT_INR,
    CALL_RECONNECT_WINDOW_SECONDS,
    CALL_REQUEST_TTL_MINUTES,
    CALL_RING_SECONDS,
    CALL_SWEEP_INTERVAL_SECONDS,
    logger,
)
from db.base import (
    CALL_HOLDING_STATUSES,
    CALL_OPEN_STATUSES,
    CallBehaviourEvent,
    CallRequest,
    TruckRoute,
    User,
)
from services import agora_client, call_push
from services import wallet as wallet_service
from services.notifications import send_push_notification

CENT = Decimal("0.01")

# What the apps may report about a call; anything else is rejected so the table
# stays queryable. Chat TEXT is never accepted, only that chat was used.
CLIENT_EVENT_TYPES = frozenset(
    {
        "request_viewed",
        "call_screen_opened",
        "ring_answered",
        "ring_declined",
        "call_joined",
        "call_left",
        "call_cut",
        "mic_on",
        "mic_off",
        "camera_on",
        "camera_off",
        "camera_switched",
        "speaker_on",
        "speaker_off",
        "chat_opened",
        "chat_closed",
        "chat_message_sent",
        "reconnect_offered",
        "reconnect_accepted",
        "reconnect_declined",
        "network_lost",
        "network_restored",
        "app_backgrounded",
        "app_foregrounded",
        # Where the phone's audio/video setup got to (step names and SDK error codes, never content).
        "media_diagnostic",
    }
)
MAX_EVENTS_PER_BATCH = 50
MAX_EVENTS_PER_REQUEST = 1000
MAX_EVENT_DETAILS_CHARS = 1000


class CallRequestError(Exception):
    """A rule-violation the app can act on. `code` is stable (the app switches on
    it); `message` is shown to the user; `extra` carries numbers (e.g. the
    shortfall behind INSUFFICIENT_BALANCE). The app only reads `detail` as a
    string, so the structured part rides alongside it, not inside it."""

    def __init__(self, status_code: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"detail": self.message, "code": self.code, **self.extra}


def _now() -> datetime:
    return datetime.utcnow()


def money(value: Decimal) -> str:
    return f"{value.quantize(CENT):f}"


def compute_hold(amount: Decimal) -> Decimal:
    """The call amount: max(25% of the offer, Rs25), never more than the offer."""
    quarter = (amount * CALL_HOLD_FRACTION).quantize(CENT, rounding=ROUND_HALF_UP)
    return min(max(quarter, CALL_HOLD_MIN_INR), amount)


def role_of(request: CallRequest, user_id: UUID) -> str | None:
    if user_id == request.requester_id:
        return "requester"
    if user_id == request.provider_id:
        return "provider"
    return None


def _naive_utc(moment: datetime | None) -> datetime | None:
    """A phone's time as the naive UTC the database stores. The apps send "...Z" or an offset
    (timezone-aware); a time in the future is a wrong phone clock, so it counts as "now"."""
    if moment is None:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return min(moment, _now())


def add_event(
    session: AsyncSession,
    request: CallRequest,
    user_id: UUID,
    event_type: str,
    *,
    source: str = "server",
    occurred_at: datetime | None = None,
    details: dict | None = None,
    role: str | None = None,
) -> None:
    """`role="system"` marks events nobody did (expiry, a call that never
    connected); they hang off the requester only because the column is required."""
    session.add(
        CallBehaviourEvent(
            call_request_id=request.id,
            user_id=user_id,
            role=role or role_of(request, user_id) or "system",
            event_type=event_type,
            source=source,
            occurred_at=_naive_utc(occurred_at) or _now(),
            details=details,
        )
    )


async def _notify(session: AsyncSession, user_id: UUID, title: str, body: str, data: dict) -> None:
    """Best-effort push. Never lets a notification problem undo money movement."""
    try:
        await send_push_notification(str(user_id), title, body, {k: str(v) for k, v in data.items()}, session=session)
    except Exception as e:  # noqa: BLE001
        logger.error("Call request push to %s failed: %s", user_id, e)


# The ring is short, so a slow push service must not eat it: wait this long, then carry on.
_PUSH_BUDGET_SECONDS = 6


async def _push_incoming_call(session: AsyncSession, request: CallRequest, caller_name: str) -> None:
    """Ring the requester's phones (system call screen where possible). Best-effort and time-boxed."""
    ring_end = ring_deadline(request)
    if ring_end is None:
        return
    try:
        await asyncio.wait_for(
            call_push.notify_incoming_call(session, request, caller_name, ring_end), _PUSH_BUDGET_SECONDS
        )
    except Exception as e:  # noqa: BLE001 - includes the timeout
        logger.error("Incoming-call push for request %s failed or timed out: %s", request.id, e)


async def _push_call_cancelled(session: AsyncSession, request: CallRequest) -> None:
    try:
        await asyncio.wait_for(call_push.notify_call_cancelled(session, request), _PUSH_BUDGET_SECONDS)
    except Exception as e:  # noqa: BLE001
        logger.error("Call-cancelled push for request %s failed or timed out: %s", request.id, e)


_background_tasks: set[asyncio.Task] = set()


def _after_response(session: AsyncSession, label: str, work) -> None:
    """Run best-effort `work(own_session)` without holding up the answer to the app (a person who just
    tapped Answer must not wait on a push service). It gets its own session: the request's is closed
    by then. Tests await `drain_background()`."""
    bind = session.bind

    async def run() -> None:
        try:
            async with AsyncSession(bind=bind, expire_on_commit=False) as own:
                await asyncio.wait_for(work(own), _PUSH_BUDGET_SECONDS)
        except Exception as e:  # noqa: BLE001 - includes the timeout
            logger.error("%s failed or timed out: %s", label, e)

    task = asyncio.create_task(run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def drain_background() -> None:
    if _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)


def _stop_other_phones_ringing(session: AsyncSession, request: CallRequest, except_device_id: str | None) -> None:
    """The ring was answered or declined on one phone of the requester: the others must stop ringing.
    (If we don't know which phone acted, a push to all would also silence the one that answered, so none.)"""
    if except_device_id is None:
        return
    request_id, user_id = request.id, request.requester_id
    _after_response(
        session,
        f"Stopping other phones ringing for request {request_id}",
        lambda own: call_push.notify_ring_stopped(own, request_id, user_id, except_device_id=except_device_id),
    )


async def _push_missed_call(session: AsyncSession, request: CallRequest) -> None:
    """Tell the requester they were called and didn't pick up. The ring push is silent when the phone
    never got it (battery saver, force-stopped app), so this visible note is how they find out."""
    name = await session.scalar(select(User.name).where(User.id == request.provider_id))
    await _notify(
        session,
        request.requester_id,
        "Missed 1:1 call",
        f"{name or 'The driver'} called you. They can try again, or message them from your request",
        {"type": "call_missed", "requestId": request.id, "missedBy": "requester"},
    )


async def _lock_request(session: AsyncSession, request_id: UUID) -> CallRequest:
    result = await session.execute(select(CallRequest).where(CallRequest.id == request_id).with_for_update())
    request = result.scalar_one_or_none()
    if request is None:
        raise CallRequestError(404, "REQUEST_NOT_FOUND", "This request doesn't exist")
    return request


def _require_party(request: CallRequest, user_id: UUID) -> str:
    role = role_of(request, user_id)
    if role is None:
        # Same answer as a missing request: don't confirm the id exists to outsiders.
        raise CallRequestError(404, "REQUEST_NOT_FOUND", "This request doesn't exist")
    return role


def _wrong_state(request: CallRequest, action: str) -> CallRequestError:
    return CallRequestError(
        409,
        "INVALID_STATE",
        f"This request is {request.status.replace('_', ' ')} and can't be {action}",
        status=request.status,
    )


# --- ending a request without a charge --------------------------------------


async def _end_without_charge(
    session: AsyncSession, request: CallRequest, new_status: str, event_type: str, actor_id: UUID | None
) -> None:
    """Move to a terminal no-charge status and give the hold back. The caller holds
    the request lock and has checked the current status, and commits afterwards."""
    now = _now()
    request.status = new_status
    request.responded_at = request.responded_at or now
    request.updated_at = now
    await wallet_service.release_call_hold(session, request, note=event_type)
    if actor_id is None:
        add_event(session, request, request.requester_id, event_type, role="system")
    else:
        add_event(session, request, actor_id, event_type)


async def _expire_now(session: AsyncSession, request: CallRequest) -> None:
    await _end_without_charge(session, request, "expired", "request_expired", None)
    await session.commit()


# --- create / read ------------------------------------------------------------


async def create_request(
    session: AsyncSession,
    requester: User,
    provider_id: UUID,
    truck_route_id: UUID,
    topic: str,
    duration_minutes: int,
    amount: Decimal,
) -> CallRequest:
    amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    topic = topic.strip()
    if not topic:
        raise CallRequestError(400, "INVALID_TOPIC", "Tell the driver what you want to discuss")
    if duration_minutes not in CALL_DURATION_OPTIONS_MINUTES:
        raise CallRequestError(400, "INVALID_DURATION", "Pick one of the listed call durations")
    if amount < CALL_MIN_AMOUNT_INR or amount > CALL_MAX_AMOUNT_INR:
        raise CallRequestError(
            400,
            "INVALID_AMOUNT",
            f"Offer between ₹{money(CALL_MIN_AMOUNT_INR)} and ₹{money(CALL_MAX_AMOUNT_INR)}",
            minAmount=money(CALL_MIN_AMOUNT_INR),
            maxAmount=money(CALL_MAX_AMOUNT_INR),
        )
    if provider_id == requester.id:
        raise CallRequestError(400, "SELF_REQUEST", "You can't request a call with yourself")

    provider = await session.get(User, provider_id)
    if provider is None or provider.account_status == "suspended":
        raise CallRequestError(404, "PROVIDER_UNAVAILABLE", "This driver is not available")

    # Requests are made from a live truck post (the heatmap pin / search result),
    # so a user id alone is never enough to ring someone.
    post = await session.get(TruckRoute, truck_route_id)
    if post is None or post.user_id != provider_id or post.expires_at <= _now():
        raise CallRequestError(404, "TRUCK_POST_UNAVAILABLE", "This truck post is no longer available")

    # Lock the requester's wallet before looking at anything money-related: every
    # request creation and every redeem takes this same lock, so the balance check
    # below can't be overtaken by a concurrent request or withdrawal.
    wallet = await wallet_service._locked_wallet(session, requester.id)
    if wallet.status == "frozen":
        raise CallRequestError(403, "WALLET_FROZEN", "Your wallet is frozen, contact support")

    existing = await session.scalar(
        select(CallRequest.id).where(
            CallRequest.requester_id == requester.id,
            CallRequest.provider_id == provider_id,
            CallRequest.status.in_(CALL_OPEN_STATUSES),
        )
    )
    if existing:
        raise CallRequestError(
            409,
            "ALREADY_OPEN",
            "You already have an open request with this driver",
            requestId=str(existing),
        )

    committed = await wallet_service.call_commitments(session, requester.id)
    spendable = wallet.available_balance - committed
    if spendable < amount:
        raise CallRequestError(
            402,
            "INSUFFICIENT_BALANCE",
            f"Add ₹{money(amount - spendable)} to your wallet to offer ₹{money(amount)}",
            requiredAmount=money(amount),
            spendableBalance=money(max(spendable, Decimal(0))),
            shortfall=money(amount - spendable),
        )

    now = _now()
    request = CallRequest(
        requester_id=requester.id,
        provider_id=provider_id,
        truck_route_id=truck_route_id,
        topic=topic,
        duration_minutes=duration_minutes,
        amount=amount,
        hold_amount=compute_hold(amount),
        status="pending",
        expires_at=now + timedelta(minutes=CALL_REQUEST_TTL_MINUTES),
    )
    session.add(request)
    await session.flush()  # the id is the ledger row's reference
    await wallet_service.hold_for_call(session, request, wallet)
    add_event(session, request, requester.id, "request_created", details={"amount": money(amount)})
    try:
        await session.commit()
    except IntegrityError:
        # Lost a race on the one-open-request-per-pair index.
        await session.rollback()
        raise CallRequestError(409, "ALREADY_OPEN", "You already have an open request with this driver")
    await session.refresh(request)

    await _notify(
        session,
        provider_id,
        "New 1:1 call request",
        f"{requester.name or 'Someone'} offered ₹{money(amount)} for a {duration_minutes} min call",
        {"type": "call_request", "requestId": request.id},
    )
    return request


async def get_request(session: AsyncSession, request_id: UUID, user_id: UUID) -> CallRequest:
    request = await session.get(CallRequest, request_id)
    if request is None:
        raise CallRequestError(404, "REQUEST_NOT_FOUND", "This request doesn't exist")
    _require_party(request, user_id)
    return request


async def list_incoming(session: AsyncSession, provider_id: UUID) -> list[CallRequest]:
    """The provider's queue, oldest first: new requests to review, plus accepted
    ones still waiting for the provider to start the call. Requests past their
    window are left out even if the sweeper hasn't flipped them yet."""
    result = await session.execute(
        select(CallRequest)
        .where(
            CallRequest.provider_id == provider_id,
            CallRequest.status.in_(CALL_OPEN_STATUSES),
            CallRequest.expires_at > _now(),
        )
        .order_by(CallRequest.created_at.asc())
    )
    return list(result.scalars().all())


async def list_outgoing(session: AsyncSession, requester_id: UUID, *, active_only: bool, limit: int = 50) -> list[CallRequest]:
    query = select(CallRequest).where(CallRequest.requester_id == requester_id)
    if active_only:
        query = query.where(CallRequest.status.in_(CALL_OPEN_STATUSES + ("awaiting_feedback",)))
    result = await session.execute(query.order_by(CallRequest.created_at.desc()).limit(limit))
    return list(result.scalars().all())


# --- provider / requester decisions -----------------------------------------------


async def accept(session: AsyncSession, request_id: UUID, provider_id: UUID) -> CallRequest:
    request = await _lock_request(session, request_id)
    if _require_party(request, provider_id) != "provider":
        raise CallRequestError(403, "NOT_PROVIDER", "Only the driver can accept this request")
    if request.status != "pending":
        raise _wrong_state(request, "accepted")
    if request.expires_at <= _now():
        await _expire_now(session, request)
        raise CallRequestError(410, "REQUEST_EXPIRED", "This request has expired")

    request.status = "accepted"
    request.responded_at = _now()
    add_event(session, request, provider_id, "request_accepted")
    await session.commit()
    await session.refresh(request)

    provider_name = await session.scalar(select(User.name).where(User.id == provider_id))
    await _notify(
        session,
        request.requester_id,
        "Request accepted",
        f"{provider_name or 'The driver'} accepted. They will call you shortly",
        {"type": "call_request_accepted", "requestId": request.id},
    )
    return request


async def reject(session: AsyncSession, request_id: UUID, provider_id: UUID) -> CallRequest:
    request = await _lock_request(session, request_id)
    if _require_party(request, provider_id) != "provider":
        raise CallRequestError(403, "NOT_PROVIDER", "Only the driver can reject this request")
    if request.status != "pending":
        raise _wrong_state(request, "rejected")

    await _end_without_charge(session, request, "rejected", "request_rejected", provider_id)
    await session.commit()
    await session.refresh(request)

    await _notify(
        session,
        request.requester_id,
        "Request declined",
        "The driver can't take your call right now. Your money has been released",
        {"type": "call_request_rejected", "requestId": request.id},
    )
    return request


async def cancel(session: AsyncSession, request_id: UUID, requester_id: UUID) -> CallRequest:
    request = await _lock_request(session, request_id)
    if _require_party(request, requester_id) != "requester":
        raise CallRequestError(403, "NOT_REQUESTER", "Only the requester can cancel this request")
    if request.status not in ("pending", "accepted"):
        raise _wrong_state(request, "cancelled")

    await _end_without_charge(session, request, "cancelled", "request_cancelled", requester_id)
    await session.commit()
    await session.refresh(request)
    return request


# --- call outcome + settlement -----------------------------------------------------


async def _finalize_locked(session: AsyncSession, request: CallRequest, connected_seconds: int, end_reason: str) -> None:
    """Shared by every way a live call ends. Under the threshold nothing is owed
    and the hold is released; otherwise the requester is asked if they were
    satisfied. Caller holds the request lock and commits."""
    now = _now()
    request.call_ended_at = now
    request.connected_seconds = max(connected_seconds, 0)
    request.end_reason = end_reason
    add_event(
        session,
        request,
        request.requester_id,
        "call_ended",
        role="system",
        details={"reason": end_reason, "connectedSeconds": request.connected_seconds},
    )
    if request.connected_seconds < CALL_CONNECT_THRESHOLD_SECONDS:
        await _end_without_charge(session, request, "failed", "call_not_connected", None)
        return

    request.status = "awaiting_feedback"
    request.feedback_deadline = now + timedelta(hours=CALL_FEEDBACK_TTL_HOURS)
    request.updated_at = now
    add_event(
        session,
        request,
        request.requester_id,
        "call_connected_enough",
        details={"connectedSeconds": request.connected_seconds},
    )


async def _after_finalize(session: AsyncSession, request: CallRequest) -> None:
    if request.status == "awaiting_feedback":
        await _notify(
            session,
            request.requester_id,
            "How was your call?",
            "Tell us if you were satisfied to finish payment",
            {"type": "call_feedback_needed", "requestId": request.id},
        )


async def finalize_call(
    session: AsyncSession, request_id: UUID, connected_seconds: int, end_reason: str
) -> CallRequest:
    """End a ringing/live call with an explicit connected time (the server-side
    callers below compute it from heartbeats)."""
    request = await _lock_request(session, request_id)
    if request.status not in ("ringing", "live"):
        raise _wrong_state(request, "ended")
    await _finalize_locked(session, request, connected_seconds, end_reason)
    await session.commit()
    await session.refresh(request)
    await _after_finalize(session, request)
    return request


# --- the call itself: ring, join, heartbeat, end --------------------------------------------


def hard_end(request: CallRequest) -> datetime:
    """When a live call is force-ended: the chosen duration (plus a short grace) after
    it went live. Its Agora tokens expire at the same moment. A live call always has a
    start time; the fallback only exists so one odd row can never crash the sweep and
    stall everyone else's money."""
    started = request.call_started_at or request.updated_at
    return started + timedelta(minutes=request.duration_minutes, seconds=CALL_END_GRACE_SECONDS)


def ring_deadline(request: CallRequest) -> datetime | None:
    if request.ring_started_at is None:
        return None
    return request.ring_started_at + timedelta(seconds=CALL_RING_SECONDS)


def device_conflict(request: CallRequest, role: str, device_id: str | None, now: datetime) -> bool:
    """True when ANOTHER phone of this person holds the call right now.

    The phone that answered the ring (requester) or started it (provider) is remembered. Another phone
    of the same account must stay out of a call in progress: it would join the Agora channel with the
    same uid and knock the first phone off, or hang up a call it is not part of. It may only step in
    once the holder has gone quiet (a dead battery). No device id (an older app) = nothing to compare."""
    holder = request.requester_device_id if role == "requester" else request.provider_device_id
    if device_id is None or holder is None or holder == device_id:
        return False
    if request.status == "ringing":
        deadline = ring_deadline(request)
        return deadline is not None and now < deadline
    if request.status == "live":
        last_seen = request.requester_last_seen_at if role == "requester" else request.provider_last_seen_at
        last_seen = last_seen or request.call_started_at or request.updated_at
        return now - last_seen <= timedelta(seconds=CALL_DEVICE_TAKEOVER_SECONDS)
    return False


def on_another_device(request: CallRequest, viewer_id: UUID, device_id: str | None) -> bool:
    """For the app: "this call is on your other phone" (so don't offer to rejoin it or ring for it)."""
    role = role_of(request, viewer_id)
    return role is not None and request.status in ("ringing", "live") and device_conflict(
        request, role, device_id, _now()
    )


def _other_device_error() -> CallRequestError:
    return CallRequestError(409, "CALL_ON_ANOTHER_DEVICE", "This call is on another one of your devices")


def _utc_ts(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def call_credentials(request: CallRequest, user_id: UUID) -> dict:
    """What the app needs to join the Agora channel. Minted on demand (never stored),
    so a rejoin after a drop just asks again. Tokens end at the call's hard end; while
    still ringing (no start time yet) they're sized for the longest the call can run."""
    now = _now()
    if request.call_started_at is not None:
        end = hard_end(request)
    else:
        end = now + timedelta(
            seconds=CALL_RING_SECONDS + request.duration_minutes * 60 + CALL_END_GRACE_SECONDS
        )
    not_after = _utc_ts(end)
    role = role_of(request, user_id)
    rtc_token, rtc_expires = agora_client.build_rtc_token(
        channel_name=request.channel_name, uid=agora_client.uid_for_user(user_id), not_after_ts=not_after
    )
    rtm = agora_client.build_rtm_token(account=str(user_id), not_after_ts=not_after)
    ring_end = ring_deadline(request) if request.status == "ringing" else None
    return {
        "requestId": str(request.id),
        "role": role,
        "appId": agora_client.AGORA_APP_ID,
        "channelName": request.channel_name,
        "uid": agora_client.uid_for_user(user_id),
        "rtcToken": rtc_token,
        "tokenExpiresAt": _iso_utc(rtc_expires),
        "durationSeconds": request.duration_minutes * 60,
        # The requester sends audio only; their app must not publish a camera track.
        "audioOnly": role == "requester",
        "chatAvailable": rtm is not None,
        "rtmUserId": str(user_id),
        "rtmToken": rtm[0] if rtm else None,
        "ringEndsAt": ring_end.isoformat() if ring_end else None,
    }


async def start_call(
    session: AsyncSession, request_id: UUID, provider_id: UUID, device_id: str | None = None
) -> tuple[CallRequest, dict]:
    """Driver starts the call: the requester's phone rings for CALL_RING_SECONDS.
    A missed ring can be retried until the request's 30-minute window ends. Asking
    again while it is still ringing just returns the same credentials."""
    agora_client.require_configured()  # before any state change: never ring someone for a call that can't start
    request = await _lock_request(session, request_id)
    if _require_party(request, provider_id) != "provider":
        raise CallRequestError(403, "NOT_PROVIDER", "Only the driver can start this call")
    now = _now()

    if request.status == "ringing" and now < ring_deadline(request):
        if device_conflict(request, "provider", device_id, now):
            raise _other_device_error()
        return request, call_credentials(request, provider_id)  # same ring, e.g. the app restarted
    if request.status not in ("accepted", "ringing"):  # ringing past its deadline = a missed ring: retry
        raise _wrong_state(request, "started")
    if request.expires_at <= now:
        await _expire_now(session, request)
        raise CallRequestError(410, "REQUEST_EXPIRED", "This request has expired")

    request.channel_name = request.channel_name or f"call-{request.id.hex}"
    request.status = "ringing"
    request.ring_started_at = now
    request.ring_attempts += 1
    request.updated_at = now
    request.provider_device_id = device_id
    add_event(session, request, provider_id, "call_started_by_provider", details={"attempt": request.ring_attempts})
    await session.commit()
    await session.refresh(request)

    provider_name = await session.scalar(select(User.name).where(User.id == provider_id))
    await _push_incoming_call(session, request, provider_name or "The driver")
    return request, call_credentials(request, provider_id)


async def join_call(
    session: AsyncSession, request_id: UUID, user_id: UUID, device_id: str | None = None
) -> tuple[CallRequest, dict]:
    """Requester answers the ring (the call goes live), or either side rejoins a live
    call after a drop. The driver can also fetch credentials while it rings, to start
    their camera before the requester picks up."""
    agora_client.require_configured()
    request = await _lock_request(session, request_id)
    role = _require_party(request, user_id)
    now = _now()
    if request.status in ("ringing", "live") and device_conflict(request, role, device_id, now):
        raise _other_device_error()

    answered = False
    if role == "requester":
        if request.status == "ringing":
            if now >= ring_deadline(request):
                raise CallRequestError(409, "RING_OVER", "This call wasn't answered in time")
            request.status = "live"
            request.call_started_at = now
            request.last_tick_at = now
            request.requester_last_seen_at = now
            request.requester_peer_present = False
            request.provider_peer_present = False
            request.connected_ms = 0
            request.requester_device_id = device_id
            request.updated_at = now
            add_event(session, request, user_id, "call_answered")
            await session.commit()
            await session.refresh(request)
            answered = True
        elif request.status == "live":
            _rejoin(session, request, role, user_id, device_id, now)
            await session.commit()
        else:
            raise _wrong_state(request, "joined")
    elif request.status not in ("ringing", "live"):
        raise _wrong_state(request, "joined")
    elif request.status == "live":
        _rejoin(session, request, role, user_id, device_id, now)
        await session.commit()
    if answered:
        _stop_other_phones_ringing(session, request, device_id)
    return request, call_credentials(request, user_id)


def _rejoin(
    session: AsyncSession, request: CallRequest, role: str, user_id: UUID, device_id: str | None, now: datetime
) -> None:
    """Back into a live call. If it comes from another phone of the same person (the first having gone
    quiet, or the same phone after a reinstall) that phone now holds the call."""
    details = None
    # Coming back proves this phone is alive: from here on it is the one that must be heard from.
    if role == "requester":
        request.requester_last_seen_at = now
    else:
        request.provider_last_seen_at = now
    holder = request.requester_device_id if role == "requester" else request.provider_device_id
    if device_id is not None:
        if holder is not None and holder != device_id:
            details = {"takeover": True}
        if role == "requester":
            request.requester_device_id = device_id
        else:
            request.provider_device_id = device_id
    add_event(session, request, user_id, "call_rejoined", details=details)


def _call_state(request: CallRequest, now: datetime) -> dict:
    end = request.call_started_at + timedelta(minutes=request.duration_minutes) if request.call_started_at else None
    return {
        "status": request.status,
        "connectedSeconds": request.connected_ms // 1000
        if request.status == "live"
        else (request.connected_seconds or 0),
        "remainingSeconds": max(int((end - now).total_seconds()), 0) if end and request.status == "live" else 0,
        "thresholdSeconds": CALL_CONNECT_THRESHOLD_SECONDS,
    }


async def heartbeat(
    session: AsyncSession, request_id: UUID, user_id: UUID, peer_present: bool, device_id: str | None = None
) -> dict:
    """Each app calls this every ~5 s while on the call screen, saying whether it can
    currently see the other person in the Agora channel. Time counts toward the call
    only while both sides are fresh AND both say they can see each other, so
    heartbeating from outside the channel earns nothing. The response is the state the
    app should show (it also tells an app the call already ended)."""
    request = await _lock_request(session, request_id)
    role = _require_party(request, user_id)
    now = _now()
    if request.status != "live":
        return _call_state(request, now)
    if now >= hard_end(request):
        await _finalize_locked(session, request, request.connected_ms // 1000, "time_limit")
        await session.commit()
        await session.refresh(request)
        await _after_finalize(session, request)
        return _call_state(request, now)

    if device_conflict(request, role, device_id, now):
        # Another of this person's phones is on the call: this one's beats must not count for it.
        raise _other_device_error()

    if role == "requester":
        request.requester_last_seen_at, request.requester_peer_present = now, peer_present
        other_seen, other_present = request.provider_last_seen_at, request.provider_peer_present
    else:
        request.provider_last_seen_at, request.provider_peer_present = now, peer_present
        other_seen, other_present = request.requester_last_seen_at, request.requester_peer_present

    grace = timedelta(seconds=CALL_HEARTBEAT_GRACE_SECONDS)
    both_in = peer_present and other_present and other_seen is not None and now - other_seen <= grace
    tick = request.last_tick_at or now
    if both_in:
        # Never credit more than one grace period for a single gap between beats.
        request.connected_ms += min(int((now - tick).total_seconds() * 1000), CALL_HEARTBEAT_GRACE_SECONDS * 1000)
    request.last_tick_at = now
    request.connected_seconds = request.connected_ms // 1000
    await session.commit()
    state = _call_state(request, now)
    state["peerOnline"] = other_seen is not None and now - other_seen <= grace
    return state


async def end_call(
    session: AsyncSession, request_id: UUID, user_id: UUID, device_id: str | None = None
) -> CallRequest:
    """Hang up. While ringing: the driver cancels the ring (they can start again), the
    requester declines it (the request is cancelled, money released). While live: the
    call ends and is settled from the connected time. Anything already over is
    returned unchanged, so a double tap is harmless."""
    request = await _lock_request(session, request_id)
    role = _require_party(request, user_id)
    if request.status in ("ringing", "live") and device_conflict(request, role, device_id, _now()):
        # e.g. "Decline" on a phone whose ring screen is still up after the other phone answered.
        raise _other_device_error()

    if request.status == "ringing":
        if role == "provider":
            request.status = "accepted"
            request.updated_at = _now()
            add_event(session, request, user_id, "ring_cancelled")
        else:
            await _end_without_charge(session, request, "cancelled", "ring_declined", user_id)
        await session.commit()
        await session.refresh(request)
        if role == "provider":
            await _push_call_cancelled(session, request)  # take the call screen off the requester's phone
            _after_response(
                session,
                f"Missed-call note for request {request.id}",
                lambda own, r=request: _push_missed_call(own, r),
            )
        else:
            _stop_other_phones_ringing(session, request, device_id)
        return request

    if request.status == "live":
        await _finalize_locked(session, request, request.connected_ms // 1000, f"ended_by_{role}")
        await session.commit()
        await session.refresh(request)
        await _after_finalize(session, request)
        return request

    if request.status == "pending":
        raise _wrong_state(request, "ended")
    return request


async def _settle(session: AsyncSession, request: CallRequest, satisfied: bool | None, event_type: str) -> None:
    """satisfied=True pays the full offer; False/None only the call amount."""
    pay_amount = request.amount if satisfied else request.hold_amount
    paid = await wallet_service.settle_call_payment(session, request, pay_amount)
    now = _now()
    request.status = "completed"
    request.satisfied = satisfied
    request.settled_amount = paid
    request.settled_at = now
    request.updated_at = now
    add_event(
        session,
        request,
        request.requester_id,
        event_type,
        details={"paid": money(paid), "offered": money(request.amount)},
        role="system" if satisfied is None else None,
    )


async def submit_feedback(session: AsyncSession, request_id: UUID, requester_id: UUID, satisfied: bool) -> CallRequest:
    request = await _lock_request(session, request_id)
    if _require_party(request, requester_id) != "requester":
        raise CallRequestError(403, "NOT_REQUESTER", "Only the requester can answer this")
    if request.status == "completed":
        if request.satisfied is not None and request.satisfied == satisfied:
            return request  # a retried tap: same answer, same result
        raise CallRequestError(409, "ALREADY_SETTLED", "This call has already been settled", status=request.status)
    if request.status != "awaiting_feedback":
        raise _wrong_state(request, "settled")

    await _settle(session, request, satisfied, "feedback_satisfied" if satisfied else "feedback_not_satisfied")
    await session.commit()
    await session.refresh(request)

    await _notify(
        session,
        request.provider_id,
        "Payment received",
        f"You earned ₹{money(request.settled_amount)} for your 1:1 call",
        {"type": "call_payment", "requestId": request.id},
    )
    return request


# --- behaviour events ------------------------------------------------------------------


async def record_client_events(
    session: AsyncSession, request_id: UUID, user_id: UUID, events: list[dict]
) -> int:
    request = await get_request(session, request_id, user_id)
    if len(events) > MAX_EVENTS_PER_BATCH:
        raise CallRequestError(400, "TOO_MANY_EVENTS", f"Send at most {MAX_EVENTS_PER_BATCH} events at a time")

    for event in events:
        if event.get("type") not in CLIENT_EVENT_TYPES:
            raise CallRequestError(400, "UNKNOWN_EVENT", f"Unknown event type: {event.get('type')}")
        details = event.get("details")
        if details is not None and len(str(details)) > MAX_EVENT_DETAILS_CHARS:
            raise CallRequestError(400, "EVENT_TOO_LARGE", "Event details are too large")

    stored = await session.scalar(
        select(func.count(CallBehaviourEvent.id)).where(CallBehaviourEvent.call_request_id == request.id)
    )
    if (stored or 0) + len(events) > MAX_EVENTS_PER_REQUEST:
        raise CallRequestError(429, "EVENT_LIMIT", "Too many events recorded for this call")

    for event in events:
        add_event(
            session,
            request,
            user_id,
            event["type"],
            source="client",
            occurred_at=event.get("occurredAt"),
            details=event.get("details"),
        )
    await session.commit()
    return len(events)


# --- account deletion --------------------------------------------------------------------


async def release_for_deleted_user(session: AsyncSession, user_id: UUID) -> int:
    """Called by services/user_deletion.py before a user (and, by FK cascade, their
    call requests) disappears, so no one's money is stranded:
      * deleted DRIVER: every request still holding the requester's money is released.
      * deleted REQUESTER: their wallet goes too, but a call that already connected
        still pays the driver the call amount (the one thing that was owed).
    Caller commits."""
    handled = 0
    as_provider = (
        await session.execute(
            select(CallRequest)
            .where(CallRequest.provider_id == user_id, CallRequest.status.in_(CALL_HOLDING_STATUSES))
            .with_for_update()
        )
    ).scalars().all()
    for request in as_provider:
        await wallet_service.release_call_hold(session, request, note="provider_deleted")
        handled += 1

    owed = (
        await session.execute(
            select(CallRequest)
            .where(CallRequest.requester_id == user_id, CallRequest.status == "awaiting_feedback")
            .with_for_update()
        )
    ).scalars().all()
    for request in owed:
        await _settle(session, request, None, "requester_deleted")
        handled += 1
    return handled


# --- sweeper -----------------------------------------------------------------------------


async def _claim(session: AsyncSession, request_id: UUID, *statuses: str) -> CallRequest | None:
    """Lock one request for the sweeper, or None if another process has it or it has
    moved on since the candidate list was read."""
    return (
        await session.execute(
            select(CallRequest)
            .where(CallRequest.id == request_id, CallRequest.status.in_(statuses))
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()


async def sweep(session: AsyncSession) -> dict[str, int]:
    """One pass of the background job. It
      * expires requests past their 30-minute window (hold released),
      * returns a ring nobody answered to `accepted` so the driver can try again,
      * ends live calls that ran past their time or whose other side has been silent
        longer than the reconnect window (settled from the connected time so far),
      * settles (call amount only) requests whose requester never answered the
        satisfaction prompt.
    Safe to run in several processes at once: each request is claimed with SKIP
    LOCKED and re-checked under its lock."""
    now = _now()
    counts = {"expired": 0, "missed_rings": 0, "ended_calls": 0, "settled": 0}

    ids = (
        await session.execute(
            select(CallRequest.id).where(
                CallRequest.status.in_(("pending", "accepted", "ringing")),
                CallRequest.expires_at <= now,
            )
        )
    ).scalars().all()
    for request_id in ids:
        request = await _claim(session, request_id, "pending", "accepted", "ringing")
        if request is None or request.expires_at > now:
            await session.rollback()
            continue
        await _end_without_charge(session, request, "expired", "request_expired", None)
        await session.commit()
        counts["expired"] += 1
        await _notify(
            session,
            request.requester_id,
            "Request expired",
            "The driver didn't respond in time. Your money has been released",
            {"type": "call_request_expired", "requestId": request.id},
        )

    ids = (
        await session.execute(
            select(CallRequest.id).where(
                CallRequest.status == "ringing",
                CallRequest.ring_started_at <= now - timedelta(seconds=CALL_RING_SECONDS),
            )
        )
    ).scalars().all()
    for request_id in ids:
        request = await _claim(session, request_id, "ringing")
        if request is None or ring_deadline(request) > now:
            await session.rollback()
            continue
        request.status = "accepted"
        request.updated_at = now
        add_event(session, request, request.requester_id, "ring_missed", role="system")
        await session.commit()
        counts["missed_rings"] += 1
        await _notify(
            session,
            request.provider_id,
            "Call not answered",
            "They didn't pick up. You can try again",
            {"type": "call_missed", "requestId": request.id},
        )
        await _push_missed_call(session, request)

    ids = (await session.execute(select(CallRequest.id).where(CallRequest.status == "live"))).scalars().all()
    for request_id in ids:
        request = await _claim(session, request_id, "live")
        if request is None:
            await session.rollback()
            continue
        started = request.call_started_at or request.updated_at
        silent_since = min(request.requester_last_seen_at or started, request.provider_last_seen_at or started)
        if now >= hard_end(request):
            reason = "time_limit"
        elif now - silent_since >= timedelta(seconds=CALL_RECONNECT_WINDOW_SECONDS):
            reason = "disconnected"
        else:
            await session.rollback()
            continue
        await _finalize_locked(session, request, request.connected_ms // 1000, reason)
        await session.commit()
        await session.refresh(request)
        counts["ended_calls"] += 1
        await _after_finalize(session, request)

    ids = (
        await session.execute(
            select(CallRequest.id).where(
                CallRequest.status == "awaiting_feedback", CallRequest.feedback_deadline <= now
            )
        )
    ).scalars().all()
    for request_id in ids:
        request = await _claim(session, request_id, "awaiting_feedback")
        if request is None:
            await session.rollback()
            continue
        await _settle(session, request, None, "feedback_timeout")
        await session.commit()
        counts["settled"] += 1
        await _notify(
            session,
            request.provider_id,
            "Payment received",
            f"You earned ₹{money(request.settled_amount)} for your 1:1 call",
            {"type": "call_payment", "requestId": request.id},
        )
    return counts


async def run_call_request_sweep_loop() -> None:
    """Background job started from app.py's lifespan."""
    from db import async_session

    while True:
        try:
            async with async_session() as session:
                counts = await sweep(session)
                if any(counts.values()):
                    logger.info("Call request sweep: %s", counts)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Call request sweep failed: %s", e, exc_info=True)
        await asyncio.sleep(CALL_SWEEP_INTERVAL_SECONDS)
