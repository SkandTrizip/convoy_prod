from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from services import videos

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
