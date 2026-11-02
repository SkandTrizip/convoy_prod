"""Paid 1:1 call requests (request -> accept -> hold -> call -> settle).

Rule violations raise services.call_requests.CallRequestError, turned into
`{"detail": <message>, "code": <STABLE_CODE>, ...numbers}` by the handler in
app.py. See docs/ONE_TO_ONE_CALL_API.md for the flow and the codes.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from db.base import CallRequest, User
from db.serializers import call_request_to_dict
from middleware.auth import get_current_user
from models import (
    CallBehaviourEventsRequest,
    CallFeedbackRequest,
    CallHeartbeatRequest,
    CreateCallRequestRequest,
)
from services import call_requests as svc
from services.call_requests import CallRequestError

router = APIRouter(prefix="/call-requests", tags=["call-requests"])


def _uuid(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise CallRequestError(400, "INVALID_ID", f"Invalid {what}")


async def _respond(session: AsyncSession, requests: list[CallRequest], viewer: User) -> list[dict]:
    ids = {r.requester_id for r in requests} | {r.provider_id for r in requests}
    users = {}
    if ids:
        users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).scalars()}
    return [call_request_to_dict(r, users.get(r.requester_id), users.get(r.provider_id), viewer.id) for r in requests]


async def _one(session: AsyncSession, request: CallRequest, viewer: User) -> dict:
    return {"success": True, "request": (await _respond(session, [request], viewer))[0]}


@router.post("", status_code=201)
async def create_call_request(
    body: CreateCallRequestRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Ask a driver for a paid 1:1 call. The whole offer must be in the wallet
    (otherwise `402 INSUFFICIENT_BALANCE` with the `shortfall` to recharge); only
    the call amount (`holdAmount`: 25% of the offer, at least ₹25) is frozen."""
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
):
    """The driver's queue, oldest first: requests to review plus accepted ones
    still waiting for the driver to start the call. Poll this (and on a push) to
    drive the Home banner; `count` is the queue length."""
    requests = await svc.list_incoming(session, current_user.id)
    return {"success": True, "count": len(requests), "requests": await _respond(session, requests, current_user)}


@router.get("/outgoing")
async def outgoing_requests(
    active_only: Annotated[bool, Query(alias="activeOnly")] = False,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """The caller's own requests, newest first. `activeOnly=true` keeps only the
    ones still going (open, or waiting for their "were you satisfied?" answer)."""
    requests = await svc.list_outgoing(session, current_user.id, active_only=active_only)
    return {"success": True, "requests": await _respond(session, requests, current_user)}


@router.get("/{request_id}")
async def get_call_request(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    request = await svc.get_request(session, _uuid(request_id, "request id"), current_user.id)
    return await _one(session, request, current_user)


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
    """Driver says no. The requester's frozen money is released."""
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
):
    """Driver starts the call after accepting: the requester's phone rings for 45 s
    (push `call_incoming`). Returns the driver's Agora credentials (`call`); join the
    channel and start the camera. A missed ring can be started again until the
    request's 30-minute window ends; calling this while it is still ringing returns
    the same credentials."""
    request, call = await svc.start_call(session, _uuid(request_id, "request id"), current_user.id)
    return {**await _one(session, request, current_user), "call": call}


@router.post("/{request_id}/join")
async def join_call(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Requester answers the ring: the call goes `live` and the response carries their
    Agora credentials (`call`, with `audioOnly: true`). Either side also uses this to
    rejoin a live call after a drop. `409 RING_OVER` if the ring ran out."""
    request, call = await svc.join_call(session, _uuid(request_id, "request id"), current_user.id)
    return {**await _one(session, request, current_user), "call": call}


@router.post("/{request_id}/heartbeat")
async def call_heartbeat(
    request_id: str,
    body: CallHeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Both apps call this every ~5 s on the call screen. Returns the live state:
    `status`, `connectedSeconds`, `remainingSeconds`, `peerOnline`. When `status` is no
    longer `live` the call is over: leave the channel and, for the requester,
    show the satisfaction prompt if it is `awaiting_feedback`."""
    state = await svc.heartbeat(session, _uuid(request_id, "request id"), current_user.id, body.peerPresent)
    return {"success": True, **state}


@router.post("/{request_id}/end")
async def end_call(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Hang up. Ringing: the driver cancels the ring (can start again), the requester
    declines it (request cancelled, money released). Live: the call ends and is settled
    from the connected time (under 30 s costs nothing, otherwise the requester is
    asked if they were satisfied). Already over: returned unchanged."""
    request = await svc.end_call(session, _uuid(request_id, "request id"), current_user.id)
    return await _one(session, request, current_user)


@router.post("/{request_id}/feedback")
async def call_request_feedback(
    request_id: str,
    body: CallFeedbackRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Requester's answer to "were you satisfied?" after a call that connected.
    `satisfied: true` pays the full offer, `false` only the call amount. Safe to
    retry with the same answer."""
    request = await svc.submit_feedback(session, _uuid(request_id, "request id"), current_user.id, body.satisfied)
    return await _one(session, request, current_user)


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
