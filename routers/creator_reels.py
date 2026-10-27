"""A creator's reels + aggregate stats, for profile screens.

One endpoint serves both the owner's own dashboard and anyone else's public
profile; `viewCount` / `totalViews` are only included for the owner. Heavy
lifting lives in services/creator_stats.py.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from db.base import User
from db.serializers import creator_reel_to_dict, parse_uuid
from middleware.auth import get_current_user
from services import creator_stats, follows

router = APIRouter(prefix="/users", tags=["reels"])


@router.get("/{user_id}/reels")
async def get_creator_reels(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Ready reels uploaded by `user_id`, newest first, with totals across all
    of them. `summary.totalReels` is the full count, not the page size."""
    try:
        target_id = parse_uuid(user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid user id") from e

    is_owner = target_id == current_user.id
    await follows.get_followable_user(session, target_id)

    summary = await creator_stats.get_summary(session, target_id)
    reels = await creator_stats.list_reels(session, target_id, limit=limit, offset=offset)

    summary_dict = {"totalReels": summary.total_reels, "totalLikes": summary.total_likes}
    if is_owner:
        summary_dict["totalViews"] = summary.total_views

    return {
        "success": True,
        "isOwner": is_owner,
        "summary": summary_dict,
        "reels": [creator_reel_to_dict(v, include_views=is_owner) for v in reels],
        "total": summary.total_reels,
        "limit": limit,
        "offset": offset,
    }
