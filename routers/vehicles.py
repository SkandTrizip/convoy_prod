from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import Truck, User
from db.serializers import parse_uuid, truck_to_dict
from middleware.auth import require_path_user
from models import AddVehicleRequest
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
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Get all trucks for user"""
    try:
        result = await session.execute(
            select(Truck).where(Truck.user_id == parse_uuid(user_id)).limit(100)
        )
        trucks = result.scalars().all()
        return {
            "success": True,
            "vehicles": [truck_to_dict(t) for t in trucks],
        }
    except Exception as e:
        logger.error(f"Error in list_vehicles: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
