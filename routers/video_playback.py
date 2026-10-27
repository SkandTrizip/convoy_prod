"""Public (unauthenticated) video playback endpoints.

Deliberately not behind JWT, unlike the rest of routers/videos.py: these are
referenced directly by an HLS player as plain segment/manifest URLs (same
treatment as the playbackUrl/thumbnailUrl Bunny CDN links already returned
unauthenticated alongside them), and a typical HLS player can't attach a
custom Authorization header to the requests it makes while playing.
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.serializers import parse_uuid
from services import video_prefetch

router = APIRouter(tags=["video-playback"])


@router.get("/videos/{video_id}/preview-segment")
async def get_preview_segment(video_id: str, session: AsyncSession = Depends(get_session)):
    try:
        segment = await video_prefetch.get_preview_segment(session, parse_uuid(video_id))
        return Response(content=segment.data, media_type="video/mp2t", headers={"X-Segment-Source": segment.source})
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error in get_preview_segment: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/videos/{video_id}/fast-start.m3u8")
async def get_fast_start_manifest(video_id: str, session: AsyncSession = Depends(get_session)):
    try:
        manifest = await video_prefetch.get_fast_start_manifest(session, parse_uuid(video_id))
        return Response(content=manifest, media_type="application/vnd.apple.mpegurl")
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error in get_fast_start_manifest: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
