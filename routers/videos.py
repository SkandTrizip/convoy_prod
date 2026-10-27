from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import User
from db.serializers import parse_uuid
from middleware.auth import get_current_user, require_path_user
from models import CreateVideoRequest, ReportWatchingRequest
from services import reel_social, video_prefetch, videos

router = APIRouter(tags=["videos"])


@router.post("/videos", status_code=201)
async def create_video(
    request: CreateVideoRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Create a video upload placeholder, returns direct-to-Bunny TUS upload
    credentials. The backend never receives raw video bytes."""
    try:
        result = await videos.create_upload(
            session,
            current_user.id,
            request.title,
            request.description,
            category_ids=request.categoryIds,
            hashtags=request.hashtags,
            latitude=request.latitude,
            longitude=request.longitude,
        )
        [video] = await reel_social.serialize_reels(session, [result["video"]], current_user.id)
        return {
            "success": True,
            "video": video,
            "tusUploadUrl": result["tusUploadUrl"],
            "tusHeaders": result["tusHeaders"],
            "expiresAt": result["expiresAt"].isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in create_video: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/videos")
async def list_video_feed(
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
    """Paginated feed of ready videos only, newest first."""
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
            "videos": await reel_social.serialize_reels(session, items, current_user.id),
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in list_video_feed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/videos/{video_id}")
async def get_video(
    video_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    try:
        video = await videos.get_video(session, parse_uuid(video_id))
        [serialized] = await reel_social.serialize_reels(session, [video], current_user.id)
        return {"success": True, "video": serialized}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error in get_video: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/videos/{video_id}/sync-status")
async def sync_video_status(
    video_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Manually re-checks a video's status against Bunny. Intended for
    environments where Bunny's webhook can't reach this server (e.g. local
    dev); in production Bunny calls POST /webhooks/bunny directly instead."""
    try:
        video = await videos.get_video(session, parse_uuid(video_id))
        if video.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized for this video")
        video = await videos.sync_status(session, video)
        [serialized] = await reel_social.serialize_reels(session, [video], current_user.id)
        return {"success": True, "video": serialized}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error in sync_video_status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/videos/{video_id}/watching", status_code=202)
async def report_watching(
    video_id: str,
    request: ReportWatchingRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(get_current_user),
):
    """Signals that this video is currently being watched at `feedPosition`,
    so nearby videos' first segment can be prefetched into cache."""
    try:
        result = await video_prefetch.report_watching(session, parse_uuid(video_id), request.feedPosition)
        return {
            "success": True,
            "neighborsChecked": result.neighbors_checked,
            "cacheHits": result.cache_hits,
            "jobsPublished": result.jobs_published,
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error in report_watching: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users/{user_id}/videos")
async def list_user_videos(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_path_user),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """The caller's own uploads, any status (so a creator can see their own
    pending/rejected videos, not just ready ones)."""
    try:
        items, total = await videos.list_for_user(session, parse_uuid(user_id), limit=limit, offset=offset)
        return {
            "success": True,
            "videos": await reel_social.serialize_reels(session, items, current_user.id),
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.error(f"Error in list_user_videos: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
