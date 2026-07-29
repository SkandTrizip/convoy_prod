from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import RedeemRequest, User, WalletTransaction
from db.serializers import parse_uuid
from middleware.auth import require_path_user
from models import InitiateRedeemRequest
from services.wallet import get_or_create_wallet, initiate_redeem, list_wallet_transactions

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


@router.get("/{user_id}")
async def get_wallet(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Get the caller's wallet balance."""
    try:
        wallet = await get_or_create_wallet(session, parse_uuid(user_id))
        return {"success": True, "wallet": _wallet_to_dict(wallet)}
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
