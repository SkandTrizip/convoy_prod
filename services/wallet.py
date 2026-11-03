import re
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import DEV_WALLET_CREDIT_ENABLED, DEV_WALLET_CREDIT_MAX_INR, MIN_REDEEM_AMOUNT_INR, logger
from db.base import CALL_HOLDING_STATUSES, CallRequest, RedeemRequest, User, Wallet, WalletTransaction
from db.serializers import parse_uuid

# Loose but real UPI VPA shape: handle@bank, e.g. "9876543210@ybl"
UPI_ID_PATTERN = re.compile(r"^[\w.\-]{2,64}@[a-zA-Z]{2,64}$")


def clean_upi_id(upi_id: str | None) -> str:
    """Trimmed UPI ID, or a 400 if it is empty or not shaped like handle@bank."""
    cleaned = (upi_id or "").strip()
    if not UPI_ID_PATTERN.match(cleaned):
        raise HTTPException(status_code=400, detail="Invalid UPI ID format")
    return cleaned


async def get_upi_id(session: AsyncSession, user_id: UUID) -> str | None:
    return await session.scalar(select(User.upi_id).where(User.id == user_id))


async def set_upi_id(session: AsyncSession, user_id: UUID, upi_id: str) -> str:
    """Save (or change) where this user's withdrawals are paid. Takes effect for the
    NEXT withdrawal: a pending payout keeps the UPI ID it was requested with."""
    cleaned = clean_upi_id(upi_id)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.upi_id = cleaned
    await session.commit()
    return cleaned


async def _assert_kyc_approved(session: AsyncSession, user_id: UUID) -> None:
    """Withdrawing money to a bank account needs a verified identity, so redeem
    requires approved KYC. Holding and spending balance (e.g. paying for a 1:1
    call) does not: any user can have a wallet."""
    kyc_status = await session.scalar(select(User.kyc_status).where(User.id == user_id))
    if kyc_status != "approved":
        raise HTTPException(
            status_code=403, detail="Wallet requires an approved KYC status"
        )


async def get_or_create_wallet(session: AsyncSession, user_id: UUID) -> Wallet:
    """Read-path lookup. Creates a zero-balance wallet on first access so callers
    never have to special-case a missing row."""
    result = await session.execute(select(Wallet).where(Wallet.user_id == user_id))
    wallet = result.scalar_one_or_none()
    if wallet:
        return wallet

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
    session: AsyncSession, user_id: UUID, upi_id: str | None, idempotency_key: str
) -> RedeemRequest:
    """Reserve the full available balance for payout. Money moves available ->
    reserved immediately; it only leaves reserved once an admin confirms the
    payout (mark_redeem_paid) or it's rejected back to available (reject_redeem).

    Pays to the UPI ID saved on the account; with none saved the withdrawal is refused.
    A `upi_id` passed in is checked and saved first (older app versions send it)."""
    if upi_id is not None:
        upi_id = clean_upi_id(upi_id)

    existing = await _existing_transaction(session, idempotency_key)
    if existing and existing.reference_id:
        result = await session.execute(
            select(RedeemRequest).where(RedeemRequest.id == parse_uuid(existing.reference_id))
        )
        redeem_req = result.scalar_one_or_none()
        if redeem_req:
            return redeem_req

    await _assert_kyc_approved(session, user_id)
    if upi_id is None:
        upi_id = await get_upi_id(session, user_id)
        if not upi_id:
            raise HTTPException(status_code=400, detail="Add your UPI ID before withdrawing")
    wallet = await _locked_wallet(session, user_id)
    if wallet.status == "frozen":
        raise HTTPException(status_code=403, detail="Wallet is frozen, contact support")
    # Money promised to open 1:1 call requests can't be withdrawn from under them.
    # Read under the wallet lock: creating a request takes the same lock first.
    amount = wallet.available_balance - await call_commitments(session, user_id)
    if amount < MIN_REDEEM_AMOUNT_INR:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum balance of ₹{MIN_REDEEM_AMOUNT_INR} required to redeem",
        )

    wallet.available_balance = wallet.available_balance - amount
    wallet.reserved_balance = wallet.reserved_balance + amount
    wallet.updated_at = datetime.utcnow()

    redeem_req = RedeemRequest(user_id=user_id, amount=amount, upi_id=upi_id, status="pending")
    session.add(redeem_req)
    await session.execute(update(User).where(User.id == user_id).values(upi_id=upi_id))
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


# --- Paid 1:1 calls -----------------------------------------------------------
# These run inside the CALLER's transaction and never commit, so a call's state
# change and its ledger rows land together or not at all. Each takes the wallet
# row lock first (see _locked_wallet), and uses a deterministic idempotency key
# per request so the ledger itself rejects a duplicate hold/release/settle.


async def call_commitments(session: AsyncSession, user_id: UUID) -> Decimal:
    """The part of the user's open call offers that is NOT frozen yet: for every
    request still holding money, `amount - hold_amount`. If the requester says
    "satisfied" they owe it, so it must stay out of reach of new requests and of
    redeem. Frozen holds aren't counted here, they already left `available`."""
    total = await session.scalar(
        select(func.coalesce(func.sum(CallRequest.amount - CallRequest.hold_amount), 0)).where(
            CallRequest.requester_id == user_id,
            CallRequest.status.in_(CALL_HOLDING_STATUSES),
        )
    )
    return Decimal(total or 0)


def _call_ledger_row(
    wallet: Wallet, request_id: UUID, txn_type: str, amount: Decimal, key_suffix: str, note: str | None = None
) -> WalletTransaction:
    return WalletTransaction(
        user_id=wallet.user_id,
        type=txn_type,
        amount=amount,
        available_after=wallet.available_balance,
        reserved_after=wallet.reserved_balance,
        reference_type="call_request",
        reference_id=str(request_id),
        note=note,
        idempotency_key=f"call-{request_id}-{key_suffix}",
    )


async def hold_for_call(session: AsyncSession, request: CallRequest, wallet: Wallet) -> None:
    """Freeze the request's hold_amount (available -> reserved). `wallet` must be
    the requester's, already locked by the caller (which also checked it)."""
    wallet.available_balance = wallet.available_balance - request.hold_amount
    wallet.reserved_balance = wallet.reserved_balance + request.hold_amount
    wallet.updated_at = datetime.utcnow()
    session.add(_call_ledger_row(wallet, request.id, "call_hold", request.hold_amount, "hold"))


async def release_call_hold(session: AsyncSession, request: CallRequest, note: str | None = None) -> None:
    """Give the whole hold back (reserved -> available): the request ended with
    nothing owed (rejected, cancelled, expired, call never connected)."""
    wallet = await _locked_wallet(session, request.requester_id)
    wallet.reserved_balance = wallet.reserved_balance - request.hold_amount
    wallet.available_balance = wallet.available_balance + request.hold_amount
    wallet.updated_at = datetime.utcnow()
    session.add(_call_ledger_row(wallet, request.id, "call_release", request.hold_amount, "release", note))


async def settle_call_payment(session: AsyncSession, request: CallRequest, pay_amount: Decimal) -> Decimal:
    """The requester pays `pay_amount` (the hold, or the full offer) to the provider.
    The hold is consumed first, any extra comes from `available`. Returns what was
    actually paid.

    Both wallets are locked in a fixed (id) order so two settlements touching the
    same pair of users can't deadlock. A provider's frozen wallet still receives:
    refusing would strand money the requester has already paid.
    """
    first, second = sorted([request.requester_id, request.provider_id], key=str)
    locked = {first: await _locked_wallet(session, first), second: await _locked_wallet(session, second)}
    payer, earner = locked[request.requester_id], locked[request.provider_id]

    extra = pay_amount - request.hold_amount
    if extra > payer.available_balance:
        # Shouldn't happen: call_commitments keeps `extra` out of reach of redeem and
        # of new requests. Never go negative or fail the settlement; charge what's there.
        logger.error(
            "Call %s: requester %s short by %s on settlement, charging what is available",
            request.id,
            request.requester_id,
            extra - payer.available_balance,
        )
        extra = payer.available_balance
    paid = request.hold_amount + extra

    now = datetime.utcnow()
    payer.reserved_balance = payer.reserved_balance - request.hold_amount
    payer.available_balance = payer.available_balance - extra
    payer.updated_at = now
    session.add(_call_ledger_row(payer, request.id, "call_debit", paid, "debit"))

    earner.available_balance = earner.available_balance + paid
    earner.updated_at = now
    session.add(_call_ledger_row(earner, request.id, "call_earning", paid, "earning"))
    return paid


async def dev_credit_wallet(
    session: AsyncSession, user_id: UUID, amount: Decimal, idempotency_key: str
) -> WalletTransaction:
    """DEV ONLY: put test money in the caller's own wallet until the payment
    gateway exists. Refuses unless DEV_WALLET_CREDIT_ENABLED=true."""
    if not DEV_WALLET_CREDIT_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    if amount > DEV_WALLET_CREDIT_MAX_INR:
        raise HTTPException(status_code=400, detail=f"Maximum test credit is ₹{DEV_WALLET_CREDIT_MAX_INR}")
    return await credit_wallet(
        session,
        user_id,
        amount,
        reference_type="dev_credit",
        reference_id=str(user_id),
        idempotency_key=f"dev-credit-{user_id}-{idempotency_key}",
        note="Test credit (dev only)",
    )
