from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import Truck, TruckRoute, User
from db.serializers import parse_uuid, truck_route_to_dict, truck_to_dict
from middleware.auth import require_path_user
from models import CreateTruckRequest, TruckSearchRequest
from services.spatial import make_geography_point, search_truck_routes_spatial

router = APIRouter(prefix="/trucks", tags=["trucks"])


@router.post("")
async def create_truck_listing(
    user_id: str,
    data: CreateTruckRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Post truck availability with PostGIS origin/destination."""
    try:
        user_uuid = parse_uuid(user_id)
        user_result = await session.execute(select(User).where(User.id == user_uuid))
        user = user_result.scalar_one_or_none()
        if not user or user.kyc_status != "approved":
            raise HTTPException(status_code=403, detail="KYC must be approved")

        truck_result = await session.execute(
            select(Truck).where(
                Truck.user_id == user_uuid,
                Truck.truck_number == data.truck_number.upper(),
            )
        )
        truck = truck_result.scalar_one_or_none()
        if not truck:
            raise HTTPException(status_code=404, detail="Truck not found for user")
        if truck.verification_status != "verified":
            raise HTTPException(status_code=403, detail="Truck must be verified")

        now = datetime.utcnow()
        route = TruckRoute(
            truck_id=truck.id,
            user_id=user_uuid,
            truck_number=truck.truck_number,
            truck_type=truck.truck_type,
            capacity=truck.capacity,
            origin_name=data.origin.name,
            destination_name=data.destination.name,
            origin_location=make_geography_point(data.origin.lng, data.origin.lat),
            destination_location=make_geography_point(
                data.destination.lng, data.destination.lat
            ),
            origin=data.origin.model_dump(),
            destination=data.destination.model_dump(),
            current_location=data.current_location.model_dump()
            if data.current_location
            else None,
            available_date=data.available_date,
            status="available",
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        session.add(route)
        await session.commit()
        await session.refresh(route)

        return {"success": True, "route": truck_route_to_dict(route)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in create_truck_listing: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search")
async def search_trucks(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    available_date: date,
    radius_km: float = 150,
    truck_type: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """PostGIS radius search for available trucks."""
    try:
        results = await search_truck_routes_spatial(
            session,
            origin_lat=origin_lat,
            origin_lng=origin_lng,
            destination_lat=destination_lat,
            destination_lng=destination_lng,
            radius_km=radius_km,
            available_date=available_date,
            truck_type=truck_type,
        )
        return {"success": True, "trucks": results, "count": len(results)}
    except Exception as e:
        logger.error(f"Error in search_trucks: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/search")
async def search_trucks_post(
    body: TruckSearchRequest,
    session: AsyncSession = Depends(get_session),
):
    """PostGIS radius search (JSON body)."""
    try:
        results = await search_truck_routes_spatial(
            session,
            origin_lat=body.origin_lat,
            origin_lng=body.origin_lng,
            destination_lat=body.destination_lat,
            destination_lng=body.destination_lng,
            radius_km=body.radius_km,
            available_date=body.available_date,
            truck_type=body.truck_type,
        )
        return {"success": True, "trucks": results, "count": len(results)}
    except Exception as e:
        logger.error(f"Error in search_trucks_post: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{truck_route_id}")
async def get_truck_route(
    truck_route_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get a truck route listing by ID."""
    try:
        result = await session.execute(
            select(TruckRoute).where(TruckRoute.id == parse_uuid(truck_route_id))
        )
        route = result.scalar_one_or_none()
        if not route:
            raise HTTPException(status_code=404, detail="Truck route not found")

        truck_result = await session.execute(
            select(Truck).where(Truck.id == route.truck_id)
        )
        truck = truck_result.scalar_one_or_none()

        return {
            "success": True,
            "route": truck_route_to_dict(route),
            "truck": truck_to_dict(truck) if truck else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_truck_route: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
