from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import RedeemRequest, User, WalletTransaction
from db.serializers import parse_uuid
from middleware.auth import get_current_user, require_path_user
from models import ConfirmRechargeRequest, CreateRechargeRequest, DevCreditRequest, InitiateRedeemRequest
from services import recharge as recharge_service
from services.wallet import (
    call_commitments,
    dev_credit_wallet,
    get_or_create_wallet,
    initiate_redeem,
    list_wallet_transactions,
)

router = APIRouter(prefix="/wallet", tags=["wallet"])


def _wallet_to_dict(wallet) -> dict:
    return {
        "availableBalance": str(wallet.available_balance),
        "reservedBalance": str(wallet.reserved_balance),
        "status": wallet.status,
    }


def _transaction_to_dict(txn: WalletTransaction) -> dict:
    return {
        "id": str(txn.id),
        "type": txn.type,
        "amount": str(txn.amount),
        "availableAfter": str(txn.available_after),
        "reservedAfter": str(txn.reserved_after),
        "referenceType": txn.reference_type,
        "referenceId": txn.reference_id,
        "note": txn.note,
        "createdAt": txn.created_at.isoformat(),
    }


def _redeem_request_to_dict(redeem_req: RedeemRequest) -> dict:
    return {
        "id": str(redeem_req.id),
        "amount": str(redeem_req.amount),
        "upiId": redeem_req.upi_id,
        "status": redeem_req.status,
        "createdAt": redeem_req.created_at.isoformat(),
    }


@router.get("/recharge/config")
async def recharge_config(_: User = Depends(get_current_user)):
    """Whether recharging is on, the limits, and the gateway fee (added on top of what the person
    wants in the wallet). The app shows "You pay" from these."""
    return {"success": True, **recharge_service.get_config()}


@router.post("/{user_id}/recharges", status_code=201)
async def create_recharge(
    user_id: str,
    data: CreateRechargeRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Make a Razorpay order for adding money. The response carries what the checkout needs."""
    row = await recharge_service.create_recharge(session, parse_uuid(user_id), data.amount)
    return {"success": True, "recharge": recharge_service.recharge_to_dict(row, with_checkout=True)}


@router.post("/{user_id}/recharges/confirm")
async def confirm_recharge(
    user_id: str,
    data: ConfirmRechargeRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """After paying: the server checks the payment with Razorpay and credits the wallet (once)."""
    row = await recharge_service.confirm_recharge(
        session, parse_uuid(user_id), data.orderId, data.paymentId, data.signature
    )
    return {"success": True, "recharge": recharge_service.recharge_to_dict(row)}


@router.get("/{user_id}/recharges/{order_id}")
async def get_recharge(
    user_id: str,
    order_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Where a recharge stands (the app polls this when it could not confirm right away)."""
    row = await recharge_service.get_recharge(session, parse_uuid(user_id), order_id)
    return {"success": True, "recharge": recharge_service.recharge_to_dict(row)}


@router.get("/{user_id}")
async def get_wallet(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Get the caller's wallet balance."""
    try:
        wallet = await get_or_create_wallet(session, parse_uuid(user_id))
        # Money promised to open 1:1 call offers (not frozen, but not free either):
        # `spendableBalance` is what a new offer or a redeem can actually use.
        commitments = await call_commitments(session, wallet.user_id)
        return {
            "success": True,
            "wallet": {
                **_wallet_to_dict(wallet),
                "callCommitments": str(commitments),
                "spendableBalance": str(max(wallet.available_balance - commitments, 0)),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_wallet: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{user_id}/transactions")
async def get_wallet_transactions(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Get the caller's wallet ledger, most recent first."""
    try:
        txns = await list_wallet_transactions(session, parse_uuid(user_id))
        return {"success": True, "transactions": [_transaction_to_dict(t) for t in txns]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_wallet_transactions: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/redeem")
async def redeem_wallet(
    user_id: str,
    redeem_data: InitiateRedeemRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Redeem the full available balance to a UPI ID. Reserves the balance
    immediately; an admin confirms or rejects the actual payout separately."""
    try:
        redeem_req = await initiate_redeem(
            session, parse_uuid(user_id), redeem_data.upiId, redeem_data.idempotencyKey
        )
        return {
            "success": True,
            "message": "Redeem request submitted",
            "redeemRequest": _redeem_request_to_dict(redeem_req),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in redeem_wallet: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/dev-credit")
async def dev_credit(
    user_id: str,
    body: DevCreditRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """DEV ONLY: add test money to your own wallet until the payment gateway
    exists. `404` unless the server runs with DEV_WALLET_CREDIT_ENABLED=true."""
    try:
        txn = await dev_credit_wallet(session, parse_uuid(user_id), body.amount, body.idempotencyKey)
        return {"success": True, "transaction": _transaction_to_dict(txn)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in dev_credit: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
