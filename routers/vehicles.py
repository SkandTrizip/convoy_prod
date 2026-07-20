from datetime import datetime, timedelta
from decimal import Decimal

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import Truck, TruckRoute, User
from db.serializers import parse_uuid, truck_route_to_dict, truck_to_dict
from middleware.auth import require_path_user
from models import AddVehicleRequest
from services.destinations import get_destinations_for_routes
from services.notifications import send_expo_push_notification
from services.ulip import verify_vehicle_registration

router = APIRouter(prefix="/vehicle", tags=["vehicle"])


@router.post("/add/{user_id}")
async def add_vehicle(
    user_id: str,
    vehicle_data: AddVehicleRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Add a truck for user"""
    try:
        user_uuid = parse_uuid(user_id)
        result = await session.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
        if not user or user.kyc_status != "approved":
            raise HTTPException(
                status_code=403,
                detail="KYC must be approved before adding vehicles",
            )

        existing = await session.execute(
            select(Truck).where(
                Truck.user_id == user_uuid,
                Truck.truck_number == vehicle_data.vehicleNumber,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Vehicle already exists")

        ulip_result = verify_vehicle_registration(vehicle_data.vehicleNumber)
        if not ulip_result.get("verified"):
            raise HTTPException(
                status_code=400,
                detail=ulip_result.get("message") or "Vehicle verification failed",
            )

        vahan_data = {
            "verified": True,
            "vehicleNumber": ulip_result.get("vehicleNumber"),
            "message": ulip_result.get("message"),
            **(ulip_result.get("data") or {}),
            "mock": ulip_result.get("mock", False),
        }
        verification_status = "verified"

        truck = Truck(
            user_id=user_uuid,
            truck_number=vehicle_data.vehicleNumber.upper(),
            truck_type=vehicle_data.truckType,
            capacity=vehicle_data.capacity,
            verification_status=verification_status,
            vahan_data=vahan_data,
            added_date=datetime.utcnow(),
        )
        session.add(truck)
        await session.commit()
        await session.refresh(truck)

        if verification_status == "verified":
            await send_expo_push_notification(
                user_id,
                "Vehicle Verified",
                f"Vehicle {vehicle_data.vehicleNumber} has been verified successfully.",
                session=session,
            )

        return {"success": True, "vehicle": truck_to_dict(truck)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in add_vehicle: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list/{user_id}")
async def list_vehicles(
    user_id: str,
    truckNumber: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Get all trucks for user, optionally filtered by exact truck number"""
    try:
        query = select(Truck).where(Truck.user_id == parse_uuid(user_id))
        if truckNumber:
            query = query.where(Truck.truck_number == truckNumber.strip().upper())
        result = await session.execute(query.limit(100))
        trucks = result.scalars().all()
        return {
            "success": True,
            "vehicles": [truck_to_dict(t) for t in trucks],
        }
    except Exception as e:
        logger.error(f"Error in list_vehicles: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/related-posts/{user_id}/{vehicle_id}")
async def get_related_posts(
    user_id: str,
    vehicle_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Get all active truck posts associated with a specific vehicle"""
    try:
        user_uuid = parse_uuid(user_id)
        vehicle_uuid = parse_uuid(vehicle_id)
        
        # Verify truck belongs to user
        truck_result = await session.execute(
            select(Truck).where(
                Truck.user_id == user_uuid,
                Truck.id == vehicle_uuid,
            )
        )
        if not truck_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Vehicle not found")
            
        # Fetch active or expired posts
        result = await session.execute(
            select(TruckRoute).where(
                TruckRoute.truck_id == vehicle_uuid,
                TruckRoute.status.in_(["active", "expired"])
            )
        )
        posts = result.scalars().all()

        destinations_by_route = await get_destinations_for_routes(
            session, [p.id for p in posts]
        )

        return {
            "success": True,
            "posts": [
                truck_route_to_dict(p, destinations_by_route.get(str(p.id), []))
                for p in posts
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_related_posts: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/remove/{user_id}/{vehicle_id}")
async def remove_vehicle(
    user_id: str,
    vehicle_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Remove a vehicle and its associated posts"""
    try:
        user_uuid = parse_uuid(user_id)
        vehicle_uuid = parse_uuid(vehicle_id)
        
        # Find truck
        result = await session.execute(
            select(Truck).where(
                Truck.user_id == user_uuid,
                Truck.id == vehicle_uuid,
            )
        )
        truck = result.scalar_one_or_none()
        if not truck:
            raise HTTPException(status_code=404, detail="Vehicle not found")
            
        # Find and delete associated posts (truck routes)
        posts_result = await session.execute(
            select(TruckRoute).where(
                TruckRoute.truck_id == vehicle_uuid,
            )
        )
        posts = posts_result.scalars().all()
        for post in posts:
            await session.delete(post)
            
        # Delete the truck
        await session.delete(truck)
        await session.commit()
        
        return {"success": True, "message": "Vehicle and associated posts removed successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in remove_vehicle: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
