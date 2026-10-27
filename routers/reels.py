"""Reels: the home/feed-facing view over videos, plus social actions.

Each reel comes back with its author, categories, city/state, counts and the
viewer's own `likedByMe`, so a feed card needs no second round trip. Listing
logic is shared with routers/videos.py's GET /videos via
services.videos.list_feed.

`admin_router` (category management) is mounted separately under the admin
JWT namespace — see routers/__init__.py.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import ReelCategory, User
from db.serializers import parse_uuid, reel_category_to_dict, reel_comment_to_dict
from middleware.admin_auth import get_current_admin
from middleware.auth import get_current_user
from models import CreateReelCommentRequest, ReelCategoryRequest, ShareReelRequest, UpdateReelCategoryRequest
from services import reel_social, videos, view_counter

router = APIRouter(tags=["reels"])
admin_router = APIRouter(
    prefix="/admin/reel-categories", tags=["admin"], dependencies=[Depends(get_current_admin)]
)


def _reel_id(reel_id: str):
    try:
        return parse_uuid(reel_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid reel id") from e


@router.get("/reel-categories")
async def list_reel_categories(session: AsyncSession = Depends(get_session)):
    """Active categories, in display order — for the upload picker and feed filter chips."""
    categories = await reel_social.list_categories(session)
    return {"success": True, "categories": [reel_category_to_dict(c) for c in categories]}


@router.get("/reels")
async def list_reels(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    category: Annotated[
        list[str] | None,
        Query(description="Category id or slug; repeat or comma-separate for several (matches any)"),
    ] = None,
    hashtag: Annotated[str | None, Query(description="With or without leading '#'")] = None,
):
    """Most recent ready reels, newest first, optionally filtered by category
    and/or hashtag."""
    try:
        items, total = await videos.list_feed(
            session,
            limit=limit,
            offset=offset,
            category=category,
            hashtag=hashtag,
            exclude_user_id=current_user.id,
        )
        return {
            "success": True,
            "reels": await reel_social.serialize_reels(session, items, current_user.id),
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in list_reels: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/reels/{reel_id}")
async def get_reel(
    reel_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    video = await reel_social.get_ready_video(session, _reel_id(reel_id))
    [reel] = await reel_social.serialize_reels(session, [video], current_user.id)
    return {"success": True, "reel": reel}


@router.post("/reels/{reel_id}/view", status_code=202)
async def record_reel_view(
    reel_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Call once the viewer has watched ~2-3s of the reel (or all of it, if
    shorter). Fire-and-forget: the count is buffered and lands in the creator's
    stats within a few seconds. `counted` is false for the creator's own views
    and for repeat reports of the same reel within a short window."""
    video = await reel_social.get_ready_video(session, _reel_id(reel_id))
    counted = await view_counter.record_view(session, video, current_user.id)
    return {"success": True, "counted": counted}


# --- Likes --------------------------------------------------------------------


@router.post("/reels/{reel_id}/like")
async def like_reel(
    reel_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Idempotent — liking an already-liked reel just returns the current count."""
    video = await reel_social.get_ready_video(session, _reel_id(reel_id))
    count = await reel_social.like(session, video, current_user.id)
    return {"success": True, "likedByMe": True, "likeCount": count}


@router.delete("/reels/{reel_id}/like")
async def unlike_reel(
    reel_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    video = await reel_social.get_ready_video(session, _reel_id(reel_id))
    count = await reel_social.unlike(session, video, current_user.id)
    return {"success": True, "likedByMe": False, "likeCount": count}


# --- Comments -------------------------------------------------------------------


@router.get("/reels/{reel_id}/comments")
async def list_reel_comments(
    reel_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Newest first. `canDelete` is true on the viewer's own comments, and on
    every comment when the viewer owns the reel."""
    video = await reel_social.get_ready_video(session, _reel_id(reel_id))
    rows, total = await reel_social.list_comments(session, video.id, limit=limit, offset=offset)
    is_owner = video.user_id == current_user.id
    return {
        "success": True,
        "comments": [
            reel_comment_to_dict(c, author, is_owner or c.user_id == current_user.id) for c, author in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/reels/{reel_id}/comments", status_code=201)
async def add_reel_comment(
    reel_id: str,
    request: CreateReelCommentRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    video = await reel_social.get_ready_video(session, _reel_id(reel_id))
    comment, count = await reel_social.add_comment(session, video, current_user.id, request.body)
    return {
        "success": True,
        "comment": reel_comment_to_dict(comment, current_user, True),
        "commentCount": count,
    }


@router.delete("/reels/{reel_id}/comments/{comment_id}")
async def delete_reel_comment(
    reel_id: str,
    comment_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Allowed for the comment's author or the reel's owner."""
    video = await reel_social.get_ready_video(session, _reel_id(reel_id))
    try:
        comment_uuid = parse_uuid(comment_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid comment id") from e
    count = await reel_social.delete_comment(session, video, comment_uuid, current_user.id)
    return {"success": True, "commentCount": count}


# --- Shares ---------------------------------------------------------------------


@router.post("/reels/{reel_id}/share")
async def share_reel(
    reel_id: str,
    request: ShareReelRequest | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Call after the native share sheet completes. Every call counts (repeat
    shares included) and is logged for recommendations."""
    video = await reel_social.get_ready_video(session, _reel_id(reel_id))
    count = await reel_social.record_share(
        session, video, current_user.id, request.channel if request else None
    )
    return {"success": True, "shareCount": count}


# --- Admin: category management ---------------------------------------------------


@admin_router.get("")
async def admin_list_reel_categories(session: AsyncSession = Depends(get_session)):
    """All categories, including deactivated ones."""
    categories = await reel_social.list_categories(session, include_inactive=True)
    return {"success": True, "categories": [reel_category_to_dict(c) for c in categories]}


@admin_router.post("", status_code=201)
async def admin_create_reel_category(
    request: ReelCategoryRequest, session: AsyncSession = Depends(get_session)
):
    category = ReelCategory(
        slug=request.slug, name=request.name, sort_order=request.sortOrder, is_active=request.isActive
    )
    session.add(category)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"Category slug '{request.slug}' already exists") from e
    await session.refresh(category)
    return {"success": True, "category": reel_category_to_dict(category)}


@admin_router.patch("/{category_id}")
async def admin_update_reel_category(
    category_id: str, request: UpdateReelCategoryRequest, session: AsyncSession = Depends(get_session)
):
    """Slug is immutable (clients filter by it). Deactivate instead of deleting:
    existing reels keep the tag, but it disappears from the picker."""
    try:
        category = await session.get(ReelCategory, parse_uuid(category_id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid category id") from e
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")

    if request.name is not None:
        category.name = request.name
    if request.sortOrder is not None:
        category.sort_order = request.sortOrder
    if request.isActive is not None:
        category.is_active = request.isActive
    await session.commit()
    await session.refresh(category)
    return {"success": True, "category": reel_category_to_dict(category)}
