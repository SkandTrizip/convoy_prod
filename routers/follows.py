"""Follow / unfollow users, and follower/following lists.

Any logged-in user can view anyone's counts and lists (profiles are public to
the app's users, same as reel authors). `isMe` / `followedByMe` on each entry
are relative to the caller so a client can render the right button.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from db.base import User
from db.serializers import follow_user_to_dict, parse_uuid
from middleware.auth import get_current_user
from services import follows

router = APIRouter(prefix="/users", tags=["follows"])


def _user_id(user_id: str):
    try:
        return parse_uuid(user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid user id") from e


async def _state(session: AsyncSession, target_id, viewer_id) -> dict:
    followers, following = await follows.counts(session, target_id)
    return {
        "success": True,
        "followedByMe": await follows.is_following(session, viewer_id, target_id),
        "followerCount": followers,
        "followingCount": following,
    }


@router.post("/{user_id}/follow")
async def follow_user(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Idempotent. Returns the target's updated counts."""
    target_id = _user_id(user_id)
    await follows.follow(session, current_user.id, target_id)
    return await _state(session, target_id, current_user.id)


@router.delete("/{user_id}/follow")
async def unfollow_user(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    target_id = _user_id(user_id)
    await follows.unfollow(session, current_user.id, target_id)
    return await _state(session, target_id, current_user.id)


@router.get("/{user_id}/follow-stats")
async def get_follow_stats(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    target_id = _user_id(user_id)
    await follows.get_followable_user(session, target_id)
    return await _state(session, target_id, current_user.id)


async def _list(direction: str, user_id: str, session, current_user, limit: int, offset: int) -> dict:
    target_id = _user_id(user_id)
    await follows.get_followable_user(session, target_id)
    rows, total = await follows.list_users(
        session, target_id, current_user.id, direction=direction, limit=limit, offset=offset
    )
    return {
        "success": True,
        "users": [follow_user_to_dict(u, followed, current_user.id) for u, followed in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{user_id}/followers")
async def list_followers(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """People who follow `user_id`, most recent first."""
    return await _list("followers", user_id, session, current_user, limit, offset)


@router.get("/{user_id}/following")
async def list_following(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """People `user_id` follows, most recent first."""
    return await _list("following", user_id, session, current_user, limit, offset)
