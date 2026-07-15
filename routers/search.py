import math
from datetime import datetime, timedelta
from typing import Dict

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import CallLog, SearchDemand, User
from db.serializers import parse_uuid
from middleware.auth import get_current_user, require_path_user
from models import SearchTrucksRequest
from services.activity import record_search_activity
from services.contacts import attach_mutuals_to_listings
from services.post_expiry import expire_overdue_posts
from services.spatial import SEARCH_PAGE_SIZE, search_truck_routes_spatial

router = APIRouter(prefix="/search", tags=["search"])


@router.post("/trucks")
async def search_trucks(
    search_request: SearchTrucksRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Search for available trucks using PostGIS ST_DWithin."""
    try:
        await expire_overdue_posts(session)

        matching_posts, total_count = await search_truck_routes_spatial(
            session,
            origin_lat=search_request.origin.lat if search_request.origin else None,
            origin_lng=search_request.origin.lng if search_request.origin else None,
            destination_lat=search_request.destination.lat if search_request.destination else None,
            destination_lng=search_request.destination.lng if search_request.destination else None,
            radius_km=search_request.radius_km,
            available_date=search_request.available_date,
            truck_type=search_request.truckType,
            min_capacity=search_request.capacity,
            page=search_request.page,
        )

        matching_posts = await attach_mutuals_to_listings(
            session, current_user.id, matching_posts
        )

        # Best-effort recent-searches tracking — page excluded, it's pagination state,
        # not part of "what was searched for".
        search_criteria = search_request.model_dump(mode="json", exclude={"page"})
        await record_search_activity(session, current_user.id, search_criteria)

        return {
            "success": True,
            "posts": matching_posts,
            "page": search_request.page,
            "pageSize": SEARCH_PAGE_SIZE,
            "totalCount": total_count,
            "totalPages": math.ceil(total_count / SEARCH_PAGE_SIZE) if total_count else 0,
        }
    except Exception as e:
        logger.error(f"Error in search_trucks: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/track-demand/{user_id}")
async def track_search_demand(
    user_id: str,
    search_request: SearchTrucksRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Track failed search for smart notifications"""
    try:
        if not search_request.origin or not search_request.destination or not search_request.truckType:
            raise HTTPException(
                status_code=400,
                detail="origin, destination, and truckType are all required to track search demand",
            )

        user_uuid = parse_uuid(user_id)
        now = datetime.utcnow()

        result = await session.execute(
            select(SearchDemand).where(
                SearchDemand.user_id == user_uuid,
                SearchDemand.truck_type == search_request.truckType,
                SearchDemand.expiry_timestamp > now,
            )
        )
        existing_demand = next(
            (
                d
                for d in result.scalars().all()
                if d.origin.get("name") == search_request.origin.name
                and d.destination.get("name") == search_request.destination.name
            ),
            None,
        )

        if existing_demand:
            existing_demand.origin = search_request.origin.model_dump()
            existing_demand.destination = search_request.destination.model_dump()
            existing_demand.truck_type = search_request.truckType
            existing_demand.search_timestamp = now
            existing_demand.expiry_timestamp = now + timedelta(hours=48)
            existing_demand.notification_status = "pending"
        else:
            session.add(
                SearchDemand(
                    user_id=user_uuid,
                    origin=search_request.origin.model_dump(),
                    destination=search_request.destination.model_dump(),
                    truck_type=search_request.truckType,
                    search_timestamp=now,
                    expiry_timestamp=now + timedelta(hours=48),
                    notification_status="pending",
                )
            )

        await session.commit()
        return {"success": True, "message": "Search demand tracked"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in track_search_demand: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/log-call/{user_id}")
async def log_call_click(
    user_id: str,
    data: Dict = Body(...),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Log call button click for analytics"""
    try:
        session.add(
            CallLog(
                user_id=parse_uuid(user_id),
                truck_post_id=data.get("postId"),
                timestamp=datetime.utcnow(),
            )
        )
        await session.commit()

        return {"success": True, "message": "Call logged"}
    except Exception as e:
        logger.error(f"Error in log_call_click: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
