from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import ScratchCard, User
from db.serializers import parse_uuid, scratch_card_to_dict
from middleware.auth import require_path_user
from models import ScratchCardRevealRequest
from services.scratch_service import reveal_scratch_card

router = APIRouter(prefix="/scratch-cards", tags=["scratch_cards"])


@router.get("/{user_id}")
async def list_scratch_cards(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """List the caller's scratch cards, most recently earned first."""
    try:
        result = await session.execute(
            select(ScratchCard)
            .where(ScratchCard.user_id == parse_uuid(user_id))
            .order_by(ScratchCard.earned_at.desc())
            .limit(100)
        )
        cards = result.scalars().all()
        return {"success": True, "scratchCards": [scratch_card_to_dict(c) for c in cards]}
    except Exception as e:
        logger.error(f"Error in list_scratch_cards: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/{card_id}/scratch")
async def scratch_card(
    user_id: str,
    card_id: str,
    reveal_data: ScratchCardRevealRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Reveal a scratch card, crediting the computed reward to the wallet."""
    try:
        result = await reveal_scratch_card(
            session, parse_uuid(user_id), parse_uuid(card_id), reveal_data.idempotencyKey
        )
        return {"success": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in scratch_card: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
