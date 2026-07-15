from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import User
from middleware.auth import get_current_user
from services.activity import get_recent_posts, get_recent_searches

router = APIRouter(prefix="/history", tags=["history"])


@router.get("")
async def get_recent_activity(
    type: str = Query(..., pattern="^(search|post)$", description="'search' or 'post'"),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Get the logged-in user's last 10 searches or last 10 posts."""
    try:
        if type == "post":
            items = await get_recent_posts(session, current_user.id)
        else:
            items = await get_recent_searches(session, current_user.id)

        return {"success": True, "type": type, "items": items}
    except Exception as e:
        logger.error("Error in get_recent_activity: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
