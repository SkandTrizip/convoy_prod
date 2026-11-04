"""The admin's view of advance payments that need a human: denied or unanswered claims, refunds the driver
owes, and calls whose money needs a look (short calls the driver ended, calls that never really connected).

Nothing here moves money (UPI can't be reversed by us): an admin chases the refund outside the app and
records the verdict, which also decides whose record the request counts against
(see services/call_payment_standing.py).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import CallBehaviourEvent, CallRequest, User
from services.call_requests import CallRequestError

RESOLUTIONS = ("requester_right", "driver_right", "no_fault")
MAX_NOTE_CHARS = 1000

NEEDS_ATTENTION = or_(CallRequest.settlement.in_(("review", "refund_due")), CallRequest.status == "payment_disputed")


async def list_disputes(
    session: AsyncSession, status: str = "open", limit: int = 50, offset: int = 0
) -> tuple[list[CallRequest], int]:
    """`status` is open (no verdict yet), resolved or all. Newest first. Returns the page and the total."""
    if status not in ("open", "resolved", "all"):
        raise CallRequestError(400, "INVALID_STATUS", "status must be open, resolved or all")
    conditions = [NEEDS_ATTENTION]
    if status == "open":
        conditions.append(CallRequest.resolved_at.is_(None))
    elif status == "resolved":
        conditions.append(CallRequest.resolved_at.is_not(None))
    total = int((await session.scalar(select(func.count(CallRequest.id)).where(*conditions))) or 0)
    rows = await session.execute(
        select(CallRequest).where(*conditions).order_by(CallRequest.updated_at.desc()).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), total


async def dispute_events(session: AsyncSession, request_id: UUID, limit: int = 80) -> list[CallBehaviourEvent]:
    rows = await session.execute(
        select(CallBehaviourEvent)
        .where(CallBehaviourEvent.call_request_id == request_id)
        .order_by(CallBehaviourEvent.occurred_at.asc(), CallBehaviourEvent.created_at.asc())
        .limit(limit)
    )
    return list(rows.scalars().all())


async def resolve(
    session: AsyncSession, request_id: UUID, admin_id: str, resolution: str, note: str | None
) -> CallRequest:
    """Record an admin's verdict. May be changed later (the last verdict stands)."""
    if resolution not in RESOLUTIONS:
        raise CallRequestError(400, "INVALID_RESOLUTION", f"resolution must be one of {', '.join(RESOLUTIONS)}")
    request = (
        await session.execute(select(CallRequest).where(CallRequest.id == request_id).with_for_update())
    ).scalar_one_or_none()
    if request is None:
        raise CallRequestError(404, "REQUEST_NOT_FOUND", "This request doesn't exist")
    if request.settlement not in ("review", "refund_due") and request.status != "payment_disputed":
        raise CallRequestError(409, "NOT_A_DISPUTE", "This request has nothing to resolve", status=request.status)
    request.resolution = resolution
    request.resolved_at = datetime.utcnow()
    request.resolved_by = admin_id
    request.resolution_note = (note or "").strip()[:MAX_NOTE_CHARS] or None
    await session.commit()
    await session.refresh(request)
    return request
