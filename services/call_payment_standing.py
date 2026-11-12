"""Who can't use the paid 1:1 calls any more (advance-payment flow).

Counted, never stored (derived from call_requests every time, so nothing drifts). This module doesn't
import services.call_requests, which imports it.

As a REQUESTER (blocked from asking for calls at CALL_PAYMENT_STRIKE_LIMIT of either):
  * denied claims: payments they said they made and the driver (twice) said never arrived;
  * unpaid lapses: requests the driver accepted that ran out of time without a payment.
As a DRIVER:
  * no-shows: requests they took payment for and never rang once (blocked: requesters can't ask them
    at CALL_PAYMENT_STRIKE_LIMIT);
  * denials: payments they said never arrived. Flagged for admin review (not blocked) when they are at
    least CALL_DENIAL_FLAG_MIN and at least CALL_DENIAL_FLAG_RATE of the claims they answered;
  * unanswered claims: payments they never answered about (informational).

An admin can forgive everything so far (`reset_standing`: only what happened after it counts) and can
settle a disputed request (services/call_disputes.py): a verdict of `no_fault` removes it from both
people's counts, `requester_right` from the requester's (and counts against the driver), `driver_right`
the other way round.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import CALL_DENIAL_FLAG_MIN, CALL_DENIAL_FLAG_RATE, CALL_PAYMENT_STRIKE_LIMIT
from db.base import CallPaymentStanding, CallRequest, User


async def _reset_at(session: AsyncSession, user_id: UUID) -> datetime | None:
    return await session.scalar(select(CallPaymentStanding.reset_at).where(CallPaymentStanding.user_id == user_id))


def _denied(*, unless: tuple[str, ...]):
    """A payment the driver denied, unless an admin has since ruled one of `unless`."""
    return and_(
        CallRequest.status == "payment_disputed",
        CallRequest.closed_reason == "provider_denied",
        or_(CallRequest.resolution.is_(None), CallRequest.resolution.not_in(unless)),
    )


async def standing(session: AsyncSession, user_id: UUID) -> dict:
    reset_at = await _reset_at(session, user_id)

    async def count(*conditions, at) -> int:
        query = select(func.count(CallRequest.id)).where(*conditions)
        if reset_at is not None:
            query = query.where(at > reset_at)
        return int((await session.scalar(query)) or 0)

    # as the requester
    denied_claims = await count(
        CallRequest.requester_id == user_id,
        _denied(unless=("requester_right", "no_fault")),
        at=CallRequest.disputed_at,
    )
    unpaid_lapses = await count(
        CallRequest.requester_id == user_id,
        CallRequest.status == "expired",
        CallRequest.closed_reason == "payment_window_lapsed",
        at=CallRequest.expires_at,
    )
    # as the driver
    no_shows = await count(
        CallRequest.provider_id == user_id,
        CallRequest.settlement_reason == "driver_never_called",
        or_(CallRequest.resolution.is_(None), CallRequest.resolution.not_in(("driver_right", "no_fault"))),
        at=CallRequest.expires_at,
    )
    denials = await count(
        CallRequest.provider_id == user_id,
        _denied(unless=("driver_right", "no_fault")),
        at=CallRequest.disputed_at,
    )
    unanswered = await count(
        CallRequest.provider_id == user_id,
        CallRequest.status == "payment_disputed",
        CallRequest.closed_reason == "provider_silent",
        at=CallRequest.disputed_at,
    )
    confirmed = await count(
        CallRequest.provider_id == user_id, CallRequest.provider_confirmed.is_(True), at=CallRequest.provider_confirmed_at
    )
    answered = denials + confirmed
    rate = round(denials / answered, 2) if answered else 0.0

    requester_blocked = denied_claims >= CALL_PAYMENT_STRIKE_LIMIT or unpaid_lapses >= CALL_PAYMENT_STRIKE_LIMIT
    return {
        "strikeLimit": CALL_PAYMENT_STRIKE_LIMIT,
        "resetAt": reset_at.isoformat() if reset_at else None,
        # Kept at the top level: can this person request calls?
        "blocked": requester_blocked,
        "asRequester": {
            "deniedClaims": denied_claims,
            "unpaidLapses": unpaid_lapses,
            "blocked": requester_blocked,
        },
        "asDriver": {
            "noShows": no_shows,
            "deniedClaims": denials,
            "answeredClaims": answered,
            "denialRate": rate,
            "flagged": denials >= CALL_DENIAL_FLAG_MIN and rate >= CALL_DENIAL_FLAG_RATE,
            "unansweredClaims": unanswered,
            "blocked": no_shows >= CALL_PAYMENT_STRIKE_LIMIT,
        },
    }


async def is_requester_blocked(session: AsyncSession, user_id: UUID) -> bool:
    return (await standing(session, user_id))["asRequester"]["blocked"]


async def is_provider_blocked(session: AsyncSession, user_id: UUID) -> bool:
    return (await standing(session, user_id))["asDriver"]["blocked"]


async def reset_standing(session: AsyncSession, user_id: UUID, admin_id: str) -> dict:
    """Forgive everything so far (the user can use the calls again; new strikes count from now)."""
    now = datetime.utcnow()
    row = await session.get(CallPaymentStanding, user_id)
    if row is None:
        session.add(CallPaymentStanding(user_id=user_id, reset_at=now, reset_by=admin_id))
    else:
        row.reset_at, row.reset_by = now, admin_id
    await session.commit()
    return await standing(session, user_id)


def _severity(item: dict) -> tuple:
    r, d = item["asRequester"], item["asDriver"]
    return (
        item["blocked"] or d["blocked"],
        d["flagged"],
        r["deniedClaims"] + r["unpaidLapses"] + d["noShows"] + d["deniedClaims"],
    )


async def list_flagged(session: AsyncSession, limit: int = 100) -> list[dict]:
    """Everyone with something against them, worst first (for the admin screen)."""
    ids: set[UUID] = set()
    for column, conditions in (
        (CallRequest.requester_id, or_(CallRequest.status == "payment_disputed", CallRequest.closed_reason == "payment_window_lapsed")),
        (CallRequest.provider_id, or_(CallRequest.status == "payment_disputed", CallRequest.settlement_reason == "driver_never_called")),
    ):
        ids.update((await session.execute(select(column).where(conditions).distinct())).scalars().all())

    result = []
    for user_id in ids:
        item = await standing(session, user_id)
        r, d = item["asRequester"], item["asDriver"]
        if not (r["deniedClaims"] or r["unpaidLapses"] or d["noShows"] or d["deniedClaims"] or d["unansweredClaims"]):
            continue  # everything was forgiven or ruled in their favour
        user = await session.get(User, user_id)
        result.append(
            {"userId": str(user_id), "name": user.name if user else None, "mobile": user.mobile if user else None, **item}
        )
    result.sort(key=lambda i: _severity(i), reverse=True)
    return result[:limit]
