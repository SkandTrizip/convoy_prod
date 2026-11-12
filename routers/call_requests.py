"""Paid 1:1 call requests, new flow (request -> accept -> call -> the requester pays the driver's UPI ID).

Rule violations raise services.call_requests.CallRequestError, turned into
`{"detail": <message>, "code": <STABLE_CODE>, ...numbers}` by the handler in
app.py. See docs/ONE_TO_ONE_CALL_API.md for the flow and the codes.
"""

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from db.base import CallRequest, User
from db.serializers import call_request_to_dict, payment_ledger_entry_to_dict
from middleware.auth import get_current_user
from models import (
    CallBehaviourEventsRequest,
    CallConfirmPaymentRequest,
    CallHeartbeatRequest,
    CallPayRequest,
    CreateCallRequestRequest,
)
from services import call_payment_standing, call_requests as svc
from services.call_requests import CallRequestError

router = APIRouter(prefix="/call-requests", tags=["call-requests"])

# The phone making the call (the app sends it on every request). Lets a person signed in on several
# phones hold a call on one of them: see services.call_requests.device_conflict.
DeviceId = Annotated[str | None, Header(alias="X-Device-Id")]


def _uuid(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise CallRequestError(400, "INVALID_ID", f"Invalid {what}")


async def _respond(
    session: AsyncSession, requests: list[CallRequest], viewer: User, device_id: str | None = None
) -> list[dict]:
    ids = {r.requester_id for r in requests} | {r.provider_id for r in requests}
    users = {}
    if ids:
        users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).scalars()}
    return [
        call_request_to_dict(
            r,
            users.get(r.requester_id),
            users.get(r.provider_id),
            viewer.id,
            on_another_device=svc.on_another_device(r, viewer.id, device_id),
        )
        for r in requests
    ]


async def _one(session: AsyncSession, request: CallRequest, viewer: User, device_id: str | None = None) -> dict:
    return {"success": True, "request": (await _respond(session, [request], viewer, device_id))[0]}


@router.post("", status_code=201)
async def create_call_request(
    body: CreateCallRequestRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Ask a driver for a paid 1:1 call. No wallet, nothing is held: the requester pays the
    driver's UPI ID after the call. `409 PROVIDER_UPI_MISSING` if the driver hasn't added
    a UPI ID; `403 CALL_PAYMENTS_BLOCKED` after too many unpaid calls. `holdAmount` /
    `minimumAmount` is the least payable (25% of the offer, at least ₹25)."""
    request = await svc.create_request(
        session,
        current_user,
        _uuid(body.providerId, "driver id"),
        _uuid(body.truckRouteId, "truck post id"),
        body.topic,
        body.durationMinutes,
        body.amount,
    )
    return await _one(session, request, current_user)


@router.get("/incoming")
async def incoming_requests(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    device_id: DeviceId = None,
):
    """The driver's queue, oldest first: requests to review plus accepted ones
    still waiting for the driver to start the call. Poll this (and on a push) to
    drive the Home banner; `count` is the queue length."""
    requests = await svc.list_incoming(session, current_user.id)
    return {"success": True, "count": len(requests), "requests": await _respond(session, requests, current_user, device_id)}


@router.get("/outgoing")
async def outgoing_requests(
    active_only: Annotated[bool, Query(alias="activeOnly")] = False,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    device_id: DeviceId = None,
):
    """The caller's own requests, newest first. `activeOnly=true` keeps only the
    ones still going (pending, accepted and waiting for payment, payment claimed, ready, ringing, live)."""
    requests = await svc.list_outgoing(session, current_user.id, active_only=active_only)
    return {"success": True, "requests": await _respond(session, requests, current_user, device_id)}


@router.get("/payments")
async def payments_ledger(
    direction: Annotated[str, Query(description="received (you were the driver) or sent (you were the requester)")],
    limit: Annotated[int, Query(ge=1, le=svc.LEDGER_MAX_LIMIT)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """The caller's money history from 1:1 calls: payments the requester says they made, newest first.
    `total` / `count` cover every row, not only this page; `hasMore` says whether `offset + limit`
    reaches further."""
    page = await svc.list_payments(session, current_user.id, direction, limit=limit, offset=offset)
    requests = page["requests"]
    ids = {r.requester_id for r in requests} | {r.provider_id for r in requests}
    users = {}
    if ids:
        users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).scalars()}
    return {
        "success": True,
        "direction": direction,
        "payments": [
            payment_ledger_entry_to_dict(r, users.get(r.requester_id), users.get(r.provider_id), current_user.id)
            for r in requests
        ],
        "count": page["count"],
        "total": str(page["total"].quantize(Decimal("0.01"))),
        "hasMore": page["hasMore"],
    }


@router.get("/{request_id}")
async def get_call_request(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    device_id: DeviceId = None,
):
    request = await svc.get_request(session, _uuid(request_id, "request id"), current_user.id)
    return await _one(session, request, current_user, device_id)


@router.post("/{request_id}/accept")
async def accept_call_request(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Driver says yes. The requester is told; the driver then starts the call."""
    request = await svc.accept(session, _uuid(request_id, "request id"), current_user.id)
    return await _one(session, request, current_user)


@router.post("/{request_id}/reject")
async def reject_call_request(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Driver says no."""
    request = await svc.reject(session, _uuid(request_id, "request id"), current_user.id)
    return await _one(session, request, current_user)


@router.post("/{request_id}/cancel")
async def cancel_call_request(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Requester withdraws a request that is still `pending` or `accepted`."""
    request = await svc.cancel(session, _uuid(request_id, "request id"), current_user.id)
    return await _one(session, request, current_user)


@router.post("/{request_id}/start")
async def start_call(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    device_id: DeviceId = None,
):
    """Driver starts the call after accepting: the requester's phone rings for 45 s
    (push `call_incoming`). Returns the driver's Agora credentials (`call`); join the
    channel and start the camera. A missed ring can be started again until the
    request's 30-minute window ends; calling this while it is still ringing returns
    the same credentials."""
    request, call = await svc.start_call(
        session, _uuid(request_id, "request id"), current_user.id, device_id
    )
    return {**await _one(session, request, current_user, device_id), "call": call}


@router.post("/{request_id}/join")
async def join_call(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    device_id: DeviceId = None,
):
    """Requester answers the ring: the call goes `live` and the response carries their
    Agora credentials (`call`, with `audioOnly: true`). Either side also uses this to
    rejoin a live call after a drop. `409 RING_OVER` if the ring ran out."""
    request, call = await svc.join_call(
        session, _uuid(request_id, "request id"), current_user.id, device_id
    )
    return {**await _one(session, request, current_user, device_id), "call": call}


@router.post("/{request_id}/heartbeat")
async def call_heartbeat(
    request_id: str,
    body: CallHeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    device_id: DeviceId = None,
):
    """Both apps call this every ~5 s on the call screen. Returns the live state:
    `status`, `connectedSeconds`, `remainingSeconds`, `peerOnline`. When `status` is no
    longer `live` the call is over: leave the channel and, for the requester,
    close the call screen. `micOn`, `cameraOn` and
    `peerVideoOn` are optional and counted for the call's behaviour record."""
    state = await svc.heartbeat(
        session,
        _uuid(request_id, "request id"),
        current_user.id,
        body.peerPresent,
        device_id,
        mic_on=body.micOn,
        camera_on=body.cameraOn,
        peer_video_on=body.peerVideoOn,
    )
    return {"success": True, **state}


@router.post("/{request_id}/end")
async def end_call(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    device_id: DeviceId = None,
):
    """Hang up. Ringing: the driver cancels the ring (can start again, status back to `ready`), the
    requester declines it (request cancelled). Live: the call ends (`completed`, or `failed` if it never
    connected at all). Already over: returned unchanged."""
    request = await svc.end_call(session, _uuid(request_id, "request id"), current_user.id, device_id)
    return await _one(session, request, current_user, device_id)


@router.post("/{request_id}/payment/paid")
async def call_request_paid(
    request_id: str,
    body: CallPayRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Requester, from `accepted`: "I paid the driver's UPI ID" (after the app opened a UPI app and they
    came back). The whole offer is paid up front. The server can't see the money: it records the claim
    (`payment_claimed`), asks the driver to confirm, and the requester can no longer cancel. Safe to retry."""
    if body.amountType not in (None, "full"):
        raise CallRequestError(400, "INVALID_AMOUNT_TYPE", "The whole offer is paid up front")
    request = await svc.pay(session, _uuid(request_id, "request id"), current_user.id, body.upiReference)
    return await _one(session, request, current_user)


@router.post("/{request_id}/payment/confirm")
async def call_request_confirm_payment(
    request_id: str,
    body: CallConfirmPaymentRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Driver, from `payment_claimed`: "yes, I received it" (the call may now start: `ready`) or "no, I did
    not". A no has to carry `sure: true` (the app asks "are you sure?" first): without it the answer is
    `409 CONFIRM_REQUIRED` and nothing changes; with it the request is closed as `payment_disputed` and the
    call never starts. Answered once."""
    request = await svc.confirm_received(
        session, _uuid(request_id, "request id"), current_user.id, body.received, body.sure
    )
    return await _one(session, request, current_user)


@router.get("/payment-standing/me")
async def my_payment_standing(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """The caller's own record of unpaid calls: `blocked`, `strikes`, `strikeLimit`."""
    return {"success": True, "standing": await call_payment_standing.standing(session, current_user.id)}


@router.post("/{request_id}/events")
async def record_call_events(
    request_id: str,
    body: CallBehaviourEventsRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Either side reports what they did (mic off, camera off, chat opened, call
    cut, ...). Batch up to 50. Chat text is never sent or stored."""
    count = await svc.record_client_events(
        session,
        _uuid(request_id, "request id"),
        current_user.id,
        [e.model_dump() for e in body.events],
    )
    return {"success": True, "recorded": count}
