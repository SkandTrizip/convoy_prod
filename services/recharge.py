"""Adding money to a wallet through Razorpay.

The flow, and why each step exists:
  1. The app asks for an order for an amount it wants IN THE WALLET. The server adds the gateway fee on
     top, makes the Razorpay order, and remembers the amounts (a `wallet_recharges` row). From here on
     the amounts come from that row, never from the app.
  2. The app pays in Razorpay's checkout and gets back (order id, payment id, signature).
  3. The app sends those to /confirm. The server checks the signature, then asks Razorpay about the
     payment itself (right order, right amount, really captured) before it credits anything.
  4. Razorpay also tells the server (webhook). That covers the person paying and then losing the app
     or the connection before step 3.
Steps 3 and 4 can both happen, in any order, even at the same moment: the credit is idempotent
(ledger key `razorpay-<payment id>`) so the wallet is credited exactly once.
"""

import json
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from config import logger
from db.base import WalletRecharge
from services import razorpay_client, wallet as wallet_service
from services.call_requests import CallRequestError
from services.razorpay_client import RazorpayError

CENT = Decimal("0.01")
CREATED, PAID, REVIEW = "created", "paid", "review"
# Safety valve against a runaway app: this many unpaid orders within the window and new ones wait.
MAX_OPEN_ORDERS = 10
OPEN_ORDER_WINDOW = timedelta(minutes=10)


class RechargeError(CallRequestError):
    """A recharge rule the app can act on (same shape as call-request errors: plain `detail` + stable `code`)."""


def _now() -> datetime:
    return datetime.utcnow()


def money(value: Decimal) -> str:
    return f"{value.quantize(CENT):f}"


def paise(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# --- amounts ------------------------------------------------------------------------------


def compute_fee(credit: Decimal) -> tuple[Decimal, Decimal]:
    """(fee, total) when [credit] must land in the wallet whole. The fee is grossed up, because
    Razorpay takes its percentage of the whole payment: paying credit + fee leaves exactly `credit`
    after the gateway's cut. Rounded up to the paisa so the platform never ends up short."""
    rate = config.RECHARGE_FEE_PERCENT / 100
    total = ((credit + config.RECHARGE_FEE_FIXED_INR) / (1 - rate)).quantize(CENT, rounding=ROUND_CEILING)
    return total - credit, total


def get_config() -> dict:
    return {
        "enabled": razorpay_client.is_configured(),
        "minAmount": money(config.RECHARGE_MIN_INR),
        "maxAmount": money(config.RECHARGE_MAX_INR),
        "feePercent": str(config.RECHARGE_FEE_PERCENT),
        "feeFixed": money(config.RECHARGE_FEE_FIXED_INR),
    }


def recharge_to_dict(row: WalletRecharge, *, with_checkout: bool = False) -> dict:
    data = {
        "id": str(row.id),
        "orderId": row.razorpay_order_id,
        "status": row.status,
        "creditAmount": money(row.credit_amount),
        "feeAmount": money(row.fee_amount),
        "totalAmount": money(row.total_amount),
        "paidAt": row.paid_at.isoformat() if row.paid_at else None,
    }
    if with_checkout:
        # What Razorpay's checkout needs. The key id is public by design; the secret never leaves the server.
        data["checkout"] = {
            "keyId": config.RAZORPAY_KEY_ID,
            "orderId": row.razorpay_order_id,
            "amountPaise": paise(row.total_amount),
            "currency": "INR",
        }
    return data


# --- creating the order ---------------------------------------------------------------------


async def create_recharge(session: AsyncSession, user_id: UUID, amount: Decimal) -> WalletRecharge:
    if not razorpay_client.is_configured():
        raise RechargeError(503, "RECHARGE_UNAVAILABLE", "Adding money isn't available right now")
    credit = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if credit < config.RECHARGE_MIN_INR or credit > config.RECHARGE_MAX_INR:
        raise RechargeError(
            400,
            "INVALID_AMOUNT",
            f"You can add between ₹{money(config.RECHARGE_MIN_INR)} and ₹{money(config.RECHARGE_MAX_INR)} at a time",
            minAmount=money(config.RECHARGE_MIN_INR),
            maxAmount=money(config.RECHARGE_MAX_INR),
        )

    wallet = await wallet_service.get_or_create_wallet(session, user_id)
    if wallet.status == "frozen":
        raise RechargeError(403, "WALLET_FROZEN", "Your wallet is frozen, contact support")

    open_orders = await session.scalar(
        select(func.count())
        .select_from(WalletRecharge)
        .where(
            WalletRecharge.user_id == user_id,
            WalletRecharge.status == CREATED,
            WalletRecharge.created_at >= _now() - OPEN_ORDER_WINDOW,
        )
    )
    if (open_orders or 0) >= MAX_OPEN_ORDERS:
        raise RechargeError(429, "TOO_MANY_RECHARGES", "Please wait a few minutes before trying again")

    fee, total = compute_fee(credit)
    row = WalletRecharge(id=uuid4(), user_id=user_id, credit_amount=credit, fee_amount=fee, total_amount=total)
    try:
        order = await razorpay_client.create_order(
            paise(total), receipt=f"rch_{row.id.hex[:24]}", notes={"userId": str(user_id), "rechargeId": str(row.id)}
        )
    except RazorpayError as e:
        logger.error("Creating a Razorpay order failed for user %s: %s", user_id, e)
        raise RechargeError(502, "GATEWAY_ERROR", "The payment service didn't respond. Please try again") from e
    row.razorpay_order_id = order["id"]
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# --- crediting ----------------------------------------------------------------------------------


async def _row_by_order(session: AsyncSession, order_id: str, *, lock: bool = False) -> WalletRecharge | None:
    query = select(WalletRecharge).where(WalletRecharge.razorpay_order_id == order_id)
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


def _matches(payment: dict, row: WalletRecharge) -> bool:
    return (
        payment.get("order_id") == row.razorpay_order_id
        and payment.get("currency", "INR") == "INR"
        and int(payment.get("amount", -1)) == paise(row.total_amount)
    )


async def _flag_for_review(session: AsyncSession, order_id: str, note: str) -> None:
    """Money may have moved but we could not apply it: park it for a person, loudly."""
    await session.rollback()
    row = await _row_by_order(session, order_id, lock=True)
    if row is not None and row.status != PAID:
        row.status = REVIEW
        row.note = note[:500]
        await session.commit()
    logger.error("Recharge for order %s needs review: %s", order_id, note)


async def _credit(session: AsyncSession, order_id: str, payment_id: str) -> WalletRecharge:
    """Apply a verified payment to the wallet, exactly once however many times (and from however many
    places) this is reached."""
    row = await _row_by_order(session, order_id, lock=True)
    if row is None:
        raise RechargeError(404, "RECHARGE_NOT_FOUND", "We couldn't find that payment")
    if row.status == PAID:
        return row

    try:
        txn = await wallet_service.credit_wallet(
            session,
            row.user_id,
            row.credit_amount,
            reference_type="recharge",
            reference_id=str(row.id),
            idempotency_key=f"razorpay-{payment_id}",
            note=f"Wallet recharge (paid ₹{money(row.total_amount)} incl. gateway fee ₹{money(row.fee_amount)})",
        )
    except HTTPException as e:  # e.g. the wallet was frozen after the payment began
        await _flag_for_review(session, order_id, f"credit refused: {e.detail}")
        raise RechargeError(
            409,
            "NEEDS_REVIEW",
            "Your payment was received but couldn't be added to your wallet right now. Our team will sort it out.",
        ) from e

    # credit_wallet committed (which releases the row lock): look the row up again before updating it.
    row = await _row_by_order(session, order_id, lock=True)
    if row.status != PAID:
        row.status = PAID
        row.razorpay_payment_id = payment_id
        row.wallet_transaction_id = txn.id
        row.paid_at = _now()
        row.note = None
        await session.commit()
    await session.refresh(row)
    return row


async def confirm_recharge(
    session: AsyncSession, user_id: UUID, order_id: str, payment_id: str, signature: str
) -> WalletRecharge:
    """The app says "I paid". Believe nothing until the signature and Razorpay itself agree."""
    if not razorpay_client.verify_checkout_signature(order_id, payment_id, signature):
        raise RechargeError(400, "INVALID_SIGNATURE", "We couldn't verify this payment")

    row = await _row_by_order(session, order_id)
    if row is None or row.user_id != user_id:
        raise RechargeError(404, "RECHARGE_NOT_FOUND", "We couldn't find that payment")
    if row.status == PAID:
        return row

    try:
        payment = await razorpay_client.fetch_payment(payment_id)
        if payment.get("status") == "authorized" and _matches(payment, row):
            payment = await razorpay_client.capture_payment(payment_id, paise(row.total_amount))
    except RazorpayError as e:
        raise RechargeError(
            502, "GATEWAY_ERROR", "We couldn't confirm your payment yet. If money was taken it will show up shortly"
        ) from e

    if not _matches(payment, row):
        await _flag_for_review(session, order_id, f"payment {payment_id} does not match the order")
        raise RechargeError(409, "NEEDS_REVIEW", "This payment doesn't match your order. Our team will look into it.")
    status = payment.get("status")
    if status == "failed":
        raise RechargeError(402, "PAYMENT_FAILED", "The payment didn't go through. You haven't been charged")
    if status != "captured":
        raise RechargeError(409, "PAYMENT_PENDING", "Your payment is still being processed")
    return await _credit(session, order_id, payment_id)


async def handle_webhook(session: AsyncSession, body: bytes, signature: str) -> dict:
    """Razorpay says a payment happened. Safe to receive any number of times."""
    if not razorpay_client.webhook_configured():
        raise RechargeError(503, "WEBHOOK_UNAVAILABLE", "Webhook is not set up")
    if not razorpay_client.verify_webhook_signature(body, signature):
        raise RechargeError(400, "INVALID_SIGNATURE", "Invalid signature")

    try:
        event = json.loads(body)
    except ValueError as e:
        raise RechargeError(400, "INVALID_BODY", "Not JSON") from e
    if event.get("event") not in ("payment.captured", "order.paid"):
        return {"handled": False}

    payment = ((event.get("payload") or {}).get("payment") or {}).get("entity") or {}
    order_id, payment_id = payment.get("order_id"), payment.get("id")
    if not order_id or not payment_id:
        return {"handled": False}

    row = await _row_by_order(session, order_id)
    if row is None:
        return {"handled": False}  # not one of ours (Razorpay accounts can serve other things)
    if not _matches(payment, row):
        await _flag_for_review(session, order_id, f"webhook payment {payment_id} does not match the order")
        return {"handled": False, "review": True}
    if payment.get("status") not in ("captured", None):
        return {"handled": False}
    try:
        await _credit(session, order_id, payment_id)
    except RechargeError as e:
        if e.code == "NEEDS_REVIEW":
            return {"handled": False, "review": True}  # nothing a retry would fix
        raise
    return {"handled": True}


async def get_recharge(session: AsyncSession, user_id: UUID, order_id: str) -> WalletRecharge:
    row = await _row_by_order(session, order_id)
    if row is None or row.user_id != user_id:
        raise RechargeError(404, "RECHARGE_NOT_FOUND", "We couldn't find that payment")
    return row
