import re
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import MIN_REDEEM_AMOUNT_INR
from db.base import RedeemRequest, User, Wallet, WalletTransaction
from db.serializers import parse_uuid

# Loose but real UPI VPA shape: handle@bank, e.g. "9876543210@ybl"
UPI_ID_PATTERN = re.compile(r"^[\w.\-]{2,64}@[a-zA-Z]{2,64}$")


async def _assert_kyc_approved(session: AsyncSession, user_id: UUID) -> None:
    """Wallets are only ever created for KYC-approved users — an unverified
    account has no legitimate way to earn or redeem money. Only gates creation,
    never reads: a wallet that already exists stays usable even if the user's
    KYC status later changes."""
    kyc_status = await session.scalar(select(User.kyc_status).where(User.id == user_id))
    if kyc_status != "approved":
        raise HTTPException(
            status_code=403, detail="Wallet requires an approved KYC status"
        )


async def get_or_create_wallet(session: AsyncSession, user_id: UUID) -> Wallet:
    """Read-path lookup. Creates a zero-balance wallet on first access (for a
    KYC-approved user only) so callers never have to special-case a missing
    row for accounts approved before this feature shipped."""
    result = await session.execute(select(Wallet).where(Wallet.user_id == user_id))
    wallet = result.scalar_one_or_none()
    if wallet:
        return wallet

    await _assert_kyc_approved(session, user_id)

    wallet = Wallet(user_id=user_id)
    session.add(wallet)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        result = await session.execute(select(Wallet).where(Wallet.user_id == user_id))
        wallet = result.scalar_one()
    else:
        await session.refresh(wallet)
    return wallet


async def list_wallet_transactions(
    session: AsyncSession, user_id: UUID, limit: int = 50
) -> list[WalletTransaction]:
    result = await session.execute(
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user_id)
        .order_by(WalletTransaction.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def _locked_wallet(session: AsyncSession, user_id: UUID) -> Wallet:
    """Row-locks the wallet for the rest of the caller's transaction (released on
    commit/rollback) so two concurrent mutations on the same wallet serialize
    instead of racing. Lazily creates the row (flush, not commit — the caller's
    own commit persists it together with the ledger row, atomically)."""
    result = await session.execute(
        select(Wallet).where(Wallet.user_id == user_id).with_for_update()
    )
    wallet = result.scalar_one_or_none()
    if wallet is None:
        await _assert_kyc_approved(session, user_id)
        wallet = Wallet(user_id=user_id)
        session.add(wallet)
        await session.flush()
    return wallet


async def _existing_transaction(
    session: AsyncSession, idempotency_key: str
) -> WalletTransaction | None:
    result = await session.execute(
        select(WalletTransaction).where(WalletTransaction.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


async def credit_wallet(
    session: AsyncSession,
    user_id: UUID,
    amount: Decimal,
    reference_type: str,
    reference_id: str,
    idempotency_key: str,
    note: str | None = None,
) -> WalletTransaction:
    """Credit a user's wallet (e.g. a scratch card reward, or a manual admin
    adjustment). Retrying with the same idempotency_key is a no-op that returns
    the original transaction instead of crediting twice."""
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Credit amount must be positive")

    existing = await _existing_transaction(session, idempotency_key)
    if existing:
        return existing

    wallet = await _locked_wallet(session, user_id)
    if wallet.status == "frozen":
        raise HTTPException(status_code=403, detail="Wallet is frozen, contact support")

    wallet.available_balance = wallet.available_balance + amount
    wallet.updated_at = datetime.utcnow()

    txn = WalletTransaction(
        user_id=user_id,
        type="credit",
        amount=amount,
        available_after=wallet.available_balance,
        reserved_after=wallet.reserved_balance,
        reference_type=reference_type,
        reference_id=reference_id,
        note=note,
        idempotency_key=idempotency_key,
    )
    session.add(txn)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _existing_transaction(session, idempotency_key)
        if existing:
            return existing
        raise
    await session.refresh(txn)
    return txn


async def initiate_redeem(
    session: AsyncSession, user_id: UUID, upi_id: str, idempotency_key: str
) -> RedeemRequest:
    """Reserve the full available balance for payout. Money moves available ->
    reserved immediately; it only leaves reserved once an admin confirms the
    payout (mark_redeem_paid) or it's rejected back to available (reject_redeem)."""
    if not UPI_ID_PATTERN.match(upi_id):
        raise HTTPException(status_code=400, detail="Invalid UPI ID format")

    existing = await _existing_transaction(session, idempotency_key)
    if existing and existing.reference_id:
        result = await session.execute(
            select(RedeemRequest).where(RedeemRequest.id == parse_uuid(existing.reference_id))
        )
        redeem_req = result.scalar_one_or_none()
        if redeem_req:
            return redeem_req

    wallet = await _locked_wallet(session, user_id)
    if wallet.status == "frozen":
        raise HTTPException(status_code=403, detail="Wallet is frozen, contact support")
    if wallet.available_balance < MIN_REDEEM_AMOUNT_INR:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum balance of Rs.{MIN_REDEEM_AMOUNT_INR} required to redeem",
        )

    amount = wallet.available_balance
    wallet.available_balance = Decimal("0")
    wallet.reserved_balance = wallet.reserved_balance + amount
    wallet.updated_at = datetime.utcnow()

    redeem_req = RedeemRequest(user_id=user_id, amount=amount, upi_id=upi_id, status="pending")
    session.add(redeem_req)
    await session.flush()  # need redeem_req.id for the ledger row's reference_id

    session.add(
        WalletTransaction(
            user_id=user_id,
            type="reserve_redeem",
            amount=amount,
            available_after=wallet.available_balance,
            reserved_after=wallet.reserved_balance,
            reference_type="redeem_request",
            reference_id=str(redeem_req.id),
            idempotency_key=idempotency_key,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _existing_transaction(session, idempotency_key)
        if existing and existing.reference_id:
            result = await session.execute(
                select(RedeemRequest).where(RedeemRequest.id == parse_uuid(existing.reference_id))
            )
            redeem_req = result.scalar_one_or_none()
            if redeem_req:
                return redeem_req
        raise
    await session.refresh(redeem_req)
    return redeem_req


async def list_pending_redeems(session: AsyncSession, limit: int = 100) -> list[RedeemRequest]:
    result = await session.execute(
        select(RedeemRequest)
        .where(RedeemRequest.status == "pending")
        .order_by(RedeemRequest.created_at.asc())
        .limit(limit)
    )
    return result.scalars().all()


async def mark_redeem_paid(session: AsyncSession, redeem_id: UUID, admin_id: str) -> RedeemRequest:
    result = await session.execute(
        select(RedeemRequest).where(RedeemRequest.id == redeem_id).with_for_update()
    )
    redeem_req = result.scalar_one_or_none()
    if redeem_req is None:
        raise HTTPException(status_code=404, detail="Redeem request not found")
    if redeem_req.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"Already {redeem_req.status}, cannot mark paid"
        )

    wallet = await _locked_wallet(session, redeem_req.user_id)
    wallet.reserved_balance = wallet.reserved_balance - redeem_req.amount
    wallet.updated_at = datetime.utcnow()
    redeem_req.status = "paid"
    redeem_req.processed_at = datetime.utcnow()
    redeem_req.processed_by = admin_id

    session.add(
        WalletTransaction(
            user_id=redeem_req.user_id,
            type="confirm_payout",
            amount=redeem_req.amount,
            available_after=wallet.available_balance,
            reserved_after=wallet.reserved_balance,
            reference_type="redeem_request",
            reference_id=str(redeem_req.id),
            idempotency_key=f"payout-confirm-{redeem_req.id}",
        )
    )
    await session.commit()
    await session.refresh(redeem_req)
    return redeem_req


async def reject_redeem(
    session: AsyncSession, redeem_id: UUID, admin_id: str, reason: str | None
) -> RedeemRequest:
    result = await session.execute(
        select(RedeemRequest).where(RedeemRequest.id == redeem_id).with_for_update()
    )
    redeem_req = result.scalar_one_or_none()
    if redeem_req is None:
        raise HTTPException(status_code=404, detail="Redeem request not found")
    if redeem_req.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"Already {redeem_req.status}, cannot reject"
        )

    wallet = await _locked_wallet(session, redeem_req.user_id)
    wallet.reserved_balance = wallet.reserved_balance - redeem_req.amount
    wallet.available_balance = wallet.available_balance + redeem_req.amount
    wallet.updated_at = datetime.utcnow()
    redeem_req.status = "rejected"
    redeem_req.processed_at = datetime.utcnow()
    redeem_req.processed_by = admin_id
    redeem_req.notes = reason

    session.add(
        WalletTransaction(
            user_id=redeem_req.user_id,
            type="release_reserve",
            amount=redeem_req.amount,
            available_after=wallet.available_balance,
            reserved_after=wallet.reserved_balance,
            reference_type="redeem_request",
            reference_id=str(redeem_req.id),
            idempotency_key=f"payout-reject-{redeem_req.id}",
        )
    )
    await session.commit()
    await session.refresh(redeem_req)
    return redeem_req
