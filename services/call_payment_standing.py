"""Who keeps refusing to pay for calls they had (new call flow).

Two words, kept apart on purpose:
  * flagged: a call that reached CALL_CONNECT_THRESHOLD_SECONDS and the requester then refused to pay it
    (or never answered the payment sheet). Recorded for review, nothing is blocked by it alone.
  * strike: a flagged call where the provider's mic AND camera were also on for most of it (the
    requester can't say "I couldn't see or hear them"). CALL_PAYMENT_STRIKE_LIMIT strikes block the
    requester from requesting calls. An admin can forgive them (`reset_standing`): only strikes decided
    after the reset count.

Derived from call_requests every time (no counters to drift). This module doesn't import
services.call_requests, which imports it.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import CALL_PAYMENT_STRIKE_LIMIT
from db.base import CallPaymentStanding, CallRequest, User

UNPAID_OUTCOMES = ("refused", "expired")


async def _reset_at(session: AsyncSession, user_id: UUID) -> datetime | None:
    return await session.scalar(select(CallPaymentStanding.reset_at).where(CallPaymentStanding.user_id == user_id))


async def standing(session: AsyncSession, user_id: UUID) -> dict:
    reset_at = await _reset_at(session, user_id)

    def unpaid(*conditions):
        return select(func.count(CallRequest.id)).where(
            CallRequest.requester_id == user_id, CallRequest.payment_outcome.in_(UNPAID_OUTCOMES), *conditions
        )

    # Flags are history and stay; only strikes can be forgiven.
    flagged = await session.scalar(unpaid(CallRequest.threshold_met.is_(True)))
    strike_query = unpaid(CallRequest.counts_as_strike.is_(True))
    if reset_at is not None:
        strike_query = strike_query.where(CallRequest.payment_decided_at > reset_at)
    strikes = await session.scalar(strike_query)
    return {
        "flaggedCalls": int(flagged or 0),
        "strikes": int(strikes or 0),
        "strikeLimit": CALL_PAYMENT_STRIKE_LIMIT,
        "blocked": int(strikes or 0) >= CALL_PAYMENT_STRIKE_LIMIT,
        "resetAt": reset_at.isoformat() if reset_at else None,
    }


async def is_blocked(session: AsyncSession, user_id: UUID) -> bool:
    return (await standing(session, user_id))["blocked"]


async def reset_standing(session: AsyncSession, user_id: UUID, admin_id: str) -> dict:
    """Forgive the strikes so far (the user can request calls again; new strikes count from now)."""
    now = datetime.utcnow()
    row = await session.get(CallPaymentStanding, user_id)
    if row is None:
        session.add(CallPaymentStanding(user_id=user_id, reset_at=now, reset_by=admin_id))
    else:
        row.reset_at, row.reset_by = now, admin_id
    await session.commit()
    return await standing(session, user_id)


async def list_flagged(session: AsyncSession, limit: int = 100) -> list[dict]:
    """Everyone with at least one flagged call, worst first (for the admin screen)."""
    ids = (
        await session.execute(
            select(CallRequest.requester_id, func.count(CallRequest.id).label("n"))
            .where(CallRequest.payment_outcome.in_(UNPAID_OUTCOMES), CallRequest.threshold_met.is_(True))
            .group_by(CallRequest.requester_id)
            .order_by(func.count(CallRequest.id).desc())
            .limit(limit)
        )
    ).all()
    result = []
    for user_id, _ in ids:
        user = await session.get(User, user_id)
        result.append(
            {
                "userId": str(user_id),
                "name": user.name if user else None,
                "mobile": user.mobile if user else None,
                **await standing(session, user_id),
            }
        )
    return result
