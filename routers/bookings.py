from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import Booking, TruckRoute, User
from db.serializers import booking_to_dict, parse_uuid
from middleware.auth import require_path_user
from models import CreateBookingRequest

router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.post("")
async def create_booking(
    data: CreateBookingRequest,
    user_id: str = Query(..., description="UUID of the user making the booking"),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Book an available truck route."""
    try:
        user_uuid = parse_uuid(user_id)
        route_uuid = parse_uuid(data.truck_route_id)

        user_result = await session.execute(select(User).where(User.id == user_uuid))
        if not user_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="User not found")

        route_result = await session.execute(
            select(TruckRoute).where(TruckRoute.id == route_uuid)
        )
        route = route_result.scalar_one_or_none()
        if not route:
            raise HTTPException(status_code=404, detail="Truck route not found")
        if route.status not in ("available", "active"):
            raise HTTPException(status_code=400, detail="Truck route is not available")

        booking_price = (
            Decimal(str(data.price)) if data.price is not None else route.price
        )

        booking = Booking(
            truck_route_id=route_uuid,
            user_id=user_uuid,
            status="confirmed",
            price=booking_price,
            created_at=datetime.utcnow(),
        )
        route.status = "booked"
        session.add(booking)
        await session.commit()
        await session.refresh(booking)

        return {"success": True, "booking": booking_to_dict(booking)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in create_booking: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
