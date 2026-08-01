"""Scratch card lifecycle: earn (on a qualifying truck post) and reveal (on
user tap). Money is integer paise internally (see reward_config.py); it's
converted to the wallet's Decimal rupees only at the credit_wallet() boundary.

Reward is computed and committed to the reward/platform stats at ISSUANCE
time (maybe_create_scratch_card), not at reveal time — a card's amount is
decided the moment it's earned, exactly like a real physical scratch card
already has its prize printed underneath before anyone scratches it. This is
what lets the reveal API return instantly: it isn't computing anything, only
transitioning status and crediting a number that was already decided. Whether
the user ever actually scratches the card is irrelevant to the reward/platform
stats — an expired, never-scratched card still "counts" against those running
totals (see UserRewardStats/PlatformStats below), it just never reaches the
user's wallet (credit_wallet is only ever called from reveal_scratch_card).

Lock ordering for this feature (extends the wallet's existing convention):
UserRewardStats -> PlatformStats at issuance; ScratchCard -> Wallet (via
credit_wallet, which does its own locking) at reveal. `users` and
`truck_routes` are never locked FOR UPDATE here.
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import SCRATCH_CARD_EXPIRY_CHECK_INTERVAL_SECONDS, logger
from db.base import PlatformStats, ScratchCard, TruckRoute, User, UserRewardStats, WalletTransaction
from services import reward_config as config
from services.reward_engine import compute_reward_paise
from services.wallet import credit_wallet


def _paise_to_rupees(paise: int) -> Decimal:
    return (Decimal(paise) / Decimal(100)).quantize(Decimal("0.01"))


async def _compute_and_record_reward(session: AsyncSession, user_id: UUID, now: datetime) -> int:
    """Runs the reward algorithm once and immediately records it against the
    user/platform running totals — this is the one place those totals ever
    change. Called only from maybe_create_scratch_card, at issuance time."""
    stats = (
        await session.execute(
            select(UserRewardStats).where(UserRewardStats.user_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if stats is None:
        stats = UserRewardStats(user_id=user_id)
        session.add(stats)
        await session.flush()

    platform_stats = (
        await session.execute(
            select(PlatformStats).where(PlatformStats.id == 1).with_for_update()
        )
    ).scalar_one_or_none()
    if platform_stats is None:
        platform_stats = PlatformStats(id=1)
        session.add(platform_stats)
        await session.flush()

    reward_paise = compute_reward_paise(
        total_scratches=stats.total_scratches,
        total_reward_received_paise=stats.total_reward_received_paise,
        first_scratch_at=stats.first_scratch_at,
        platform_total_scratch_count=platform_stats.total_scratch_count,
        platform_total_reward_sum_paise=platform_stats.total_reward_sum_paise,
        now=now,
    )

    if stats.total_scratches == 0:
        stats.first_scratch_at = now

    stats.total_scratches += 1
    stats.total_reward_received_paise += reward_paise
    stats.updated_at = now
    platform_stats.total_scratch_count += 1
    platform_stats.total_reward_sum_paise += reward_paise

    return reward_paise


async def maybe_create_scratch_card(
    session: AsyncSession, user: User, route: TruckRoute
) -> ScratchCard | None:
    """Call right after a truck post commits. Creates a card only on the
    user's first qualifying post of the UTC calendar day. The caller
    (create_truck_post) has already enforced KYC-approved before letting the
    post through, so no separate eligibility check is needed here.

    The reward is computed and recorded right here, so the returned card
    already carries its reward_amount_paise — the frontend has the amount the
    instant this card is returned (from this call's response or any later
    GET /scratch-cards list), no need to wait on the reveal API to find out
    what it's worth.

    Best-effort: a failure here must never break post creation, so this
    catches everything and just logs.
    """
    try:
        today = datetime.utcnow().date()
        existing_today = await session.scalar(
            select(ScratchCard.id).where(
                ScratchCard.user_id == user.id,
                func.date(ScratchCard.earned_at) == today,
            )
        )
        if existing_today is not None:
            return None

        now = datetime.utcnow()
        reward_paise = await _compute_and_record_reward(session, user.id, now)

        card = ScratchCard(
            user_id=user.id,
            post_id=str(route.id),
            status="unscratched",
            reward_amount_paise=reward_paise,
            earned_at=now,
            expires_at=now + timedelta(days=config.CARD_EXPIRY_DAYS),
        )
        session.add(card)
        await session.commit()
        await session.refresh(card)
        return card
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in maybe_create_scratch_card: {str(e)}")
        return None


def _reveal_result(card: ScratchCard, txn: WalletTransaction) -> dict:
    return {
        "cardId": str(card.id),
        "rewardPaise": card.reward_amount_paise,
        "rewardRupees": str(_paise_to_rupees(card.reward_amount_paise)),
        "walletAvailableBalance": str(txn.available_after),
    }


async def reveal_scratch_card(
    session: AsyncSession, user_id: UUID, card_id: UUID, idempotency_key: str
) -> dict:
    """Reveal a card: credit its already-known reward_amount_paise to the
    wallet via credit_wallet(), and flip its status. Deliberately does NOT
    compute anything or touch UserRewardStats/PlatformStats — the reward was
    already decided and recorded when the card was issued (see
    maybe_create_scratch_card), so this call is just "commit the known
    number," which is what makes it fast enough to not need a loading spinner
    during the scratch animation.

    Idempotent: retrying with the same idempotency_key returns the original
    result instead of raising 409 or crediting the wallet again. A *different*
    key against an already-scratched card still gets the 409.
    """
    existing_txn = await session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.idempotency_key == idempotency_key,
            WalletTransaction.user_id == user_id,
        )
    )
    if (
        existing_txn is not None
        and existing_txn.reference_type == "scratch_card"
        and existing_txn.reference_id == str(card_id)
    ):
        card = await session.scalar(select(ScratchCard).where(ScratchCard.id == card_id))
        if card is not None:
            return _reveal_result(card, existing_txn)

    card = (
        await session.execute(
            select(ScratchCard)
            .where(ScratchCard.id == card_id, ScratchCard.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if card is None:
        raise HTTPException(status_code=404, detail="Scratch card not found")

    now = datetime.utcnow()

    if card.status == "scratched":
        raise HTTPException(status_code=409, detail="Card already scratched")

    if card.status == "expired" or now > card.expires_at:
        if card.status != "expired":
            card.status = "expired"
            await session.commit()
        raise HTTPException(status_code=410, detail="This scratch card has expired")

    card.status = "scratched"
    card.scratched_at = now

    txn = await credit_wallet(
        session,
        user_id,
        _paise_to_rupees(card.reward_amount_paise),
        reference_type="scratch_card",
        reference_id=str(card.id),
        idempotency_key=idempotency_key,
        note="Scratch card reward",
    )

    return _reveal_result(card, txn)


async def sweep_expired_scratch_cards(session: AsyncSession) -> int:
    """Mark unscratched-but-past-expiry cards as expired. No wallet-side
    reversal needed — an expired-unrevealed card never generated a reward."""
    now = datetime.utcnow()
    result = await session.execute(
        select(ScratchCard).where(
            ScratchCard.status == "unscratched", ScratchCard.expires_at < now
        )
    )
    cards = result.scalars().all()
    for card in cards:
        card.status = "expired"
    await session.commit()
    return len(cards)


async def run_scratch_card_expiry_loop() -> None:
    """Background job: expire unrevealed scratch cards periodically."""
    from db import async_session

    while True:
        try:
            async with async_session() as session:
                count = await sweep_expired_scratch_cards(session)
                if count:
                    logger.info("Auto-expired %s scratch card(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Scratch card expiry job failed: %s", e, exc_info=True)
        await asyncio.sleep(SCRATCH_CARD_EXPIRY_CHECK_INTERVAL_SECONDS)
