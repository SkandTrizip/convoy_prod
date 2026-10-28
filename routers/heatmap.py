"""Heatmaps: aggregated H3 cells of live truck locations and of reel posting
locations. See services/heatmap.py for what is (and is deliberately not)
exposed."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import User
from db.serializers import parse_uuid
from middleware.auth import get_current_user
from services.heatmap import (
    DEFAULT_PINS,
    DEFAULT_RESOLUTION,
    MAX_PINS,
    MAX_RESOLUTION,
    MIN_RESOLUTION,
    HeatmapKind,
    build_heatmap,
    build_truck_pins,
    get_live_post_detail,
)

router = APIRouter(prefix="/heatmap", tags=["heatmap"])

ResolutionQuery = Annotated[
    int,
    Query(
        ge=MIN_RESOLUTION,
        le=MAX_RESOLUTION,
        description=(
            f"H3 resolution to aggregate at ({MIN_RESOLUTION} coarse .. {MAX_RESOLUTION} fine). "
            "Pick it from the map zoom: ~0-5 -> 4, 6-7 -> 5, 8-9 -> 6, 10-11 -> 7, 12-13 -> 8, 14+ -> 9. "
            "Resolution 8 and finer only return cells with at least 3 items."
        ),
    ),
]


async def _heatmap(session: AsyncSession, kind: HeatmapKind, resolution: int) -> dict:
    try:
        return await build_heatmap(session, kind, resolution)
    except Exception as e:
        logger.error("Error building %s heatmap: %s", kind, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Heatmap is unavailable right now")


@router.get("/trucks")
async def trucks_heatmap(
    resolution: ResolutionQuery = DEFAULT_RESOLUTION,
    session: AsyncSession = Depends(get_session),
):
    """Where live (unexpired) truck posts are, as H3 cells with a count.
    No user ids and no exact coordinates are returned."""
    return await _heatmap(session, "trucks", resolution)


@router.get("/reels")
async def reels_heatmap(
    resolution: ResolutionQuery = DEFAULT_RESOLUTION,
    session: AsyncSession = Depends(get_session),
):
    """Where ready reels were posted (recent ones only — see
    HEATMAP_REEL_WINDOW_DAYS), as H3 cells with a count. Reels posted without
    location access are not counted. No user ids and no exact coordinates are
    returned."""
    return await _heatmap(session, "reels", resolution)


@router.get("/trucks/pins")
async def trucks_pins(
    min_lat: Annotated[float, Query(alias="minLat", ge=-90, le=90, description="South edge of the map box")],
    min_lng: Annotated[float, Query(alias="minLng", ge=-180, le=180, description="West edge of the map box")],
    max_lat: Annotated[float, Query(alias="maxLat", ge=-90, le=90, description="North edge of the map box")],
    max_lng: Annotated[float, Query(alias="maxLng", ge=-180, le=180, description="East edge of the map box")],
    limit: Annotated[int, Query(ge=1, le=MAX_PINS, description="Most pins to return, nearest the box centre first")] = DEFAULT_PINS,
    session: AsyncSession = Depends(get_session),
):
    """Real pins for live trucks inside a map box, for when the map is zoomed in on
    a city. Light items only (id, position, truck type, capacity); the full details
    of one truck come from `GET /heatmap/trucks/posts/{post_id}` when its pin is
    tapped. A position is where the truck was when its post was created/edited, not
    live tracking."""
    try:
        return await build_truck_pins(session, min_lat, min_lng, max_lat, max_lng, limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Error building truck pins: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Truck pins are unavailable right now")


@router.get("/trucks/posts/{post_id}")
async def truck_post_detail(
    post_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Full details of one live truck post (poster, contact, truck, origin,
    destinations, mutual contacts), in the same shape as an item of
    `POST /search/trucks`. `404` if the post is gone or expired."""
    try:
        post_uuid = parse_uuid(post_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid post id")
    try:
        post = await get_live_post_detail(session, post_uuid, current_user.id)
    except Exception as e:
        logger.error("Error loading truck post %s: %s", post_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Truck details are unavailable right now")
    if post is None:
        raise HTTPException(status_code=404, detail="This truck post is no longer available")
    return {"success": True, "post": post}
