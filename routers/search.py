from datetime import datetime, timedelta
from typing import Dict

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import CallLog, SearchDemand
from db.serializers import parse_uuid
from models import SearchTrucksRequest
from services.spatial import search_truck_routes_spatial

router = APIRouter(prefix="/search", tags=["search"])


@router.post("/trucks")
async def search_trucks(
    search_request: SearchTrucksRequest,
    session: AsyncSession = Depends(get_session),
):
    """Search for available trucks using PostGIS ST_DWithin."""
    try:
        matching_posts = await search_truck_routes_spatial(
            session,
            origin_lat=search_request.origin.lat,
            origin_lng=search_request.origin.lng,
            destination_lat=search_request.destination.lat,
            destination_lng=search_request.destination.lng,
            radius_km=search_request.radius_km,
            available_date=search_request.available_date,
            truck_type=search_request.truckType,
            include_user_info=True,
        )

        # Legacy response shape for existing frontend
        legacy_posts = []
        for item in matching_posts:
            legacy_posts.append(
                {
                    **item,
                    "origin": item["originLocation"],
                    "destination": item["destinationLocation"],
                }
            )

        return {
            "success": True,
            "posts": legacy_posts,
            "count": len(legacy_posts),
        }
    except Exception as e:
        logger.error(f"Error in search_trucks: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/track-demand/{user_id}")
async def track_search_demand(
    user_id: str,
    search_request: SearchTrucksRequest,
    session: AsyncSession = Depends(get_session),
):
    """Track failed search for smart notifications"""
    try:
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
    except Exception as e:
        logger.error(f"Error in track_search_demand: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/log-call/{user_id}")
async def log_call_click(
    user_id: str,
    data: Dict = Body(...),
    session: AsyncSession = Depends(get_session),
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
