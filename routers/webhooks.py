from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from services import recharge, videos

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class BunnyWebhookPayload(BaseModel):
    VideoLibraryId: int
    VideoGuid: str
    Status: int


@router.post("/bunny", status_code=204)
async def bunny_webhook(payload: BunnyWebhookPayload, session: AsyncSession = Depends(get_session)):
    """Receives Bunny Stream's encoding-status webhook. Public/unauthenticated
    — Bunny has no way to send our JWT. Runs the same status-sync logic as
    POST /videos/{video_id}/sync-status."""
    try:
        video = await videos.get_by_bunny_video_id(session, payload.VideoGuid)
        await videos.sync_status(session, video)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in bunny_webhook: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/razorpay")
async def razorpay_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    """Razorpay's payment webhook: credits a recharge even if the app never got to confirm it
    (the person paid and lost signal). Public: it is authenticated by Razorpay's signature on the raw
    body, which is why the body is read as bytes here. Always 200 for a genuine event (Razorpay would
    otherwise retry for a day); a failure to apply it is a 500 so that Razorpay does retry."""
    body = await request.body()
    result = await recharge.handle_webhook(session, body, request.headers.get("X-Razorpay-Signature", ""))
    return {"success": True, **result}
