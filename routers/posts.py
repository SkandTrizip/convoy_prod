from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import Truck, TruckRoute, User
from db.serializers import parse_uuid, truck_route_to_dict
from middleware.auth import authorize_user_id, get_current_user, require_path_user
from models import CreateTruckPostRequest
from services.matching import process_smart_match_notifications
from services.spatial import make_geography_point

router = APIRouter(prefix="/posts", tags=["posts"])


@router.post("/create/{user_id}")
async def create_truck_post(
    user_id: str,
    post_data: CreateTruckPostRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Create a truck availability post (stored as truck_route with PostGIS)."""
    try:
        user_uuid = parse_uuid(user_id)
        result = await session.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
        if not user or user.kyc_status != "approved":
            raise HTTPException(status_code=403, detail="KYC must be approved")

        truck_uuid = parse_uuid(post_data.vehicleId)
        truck_result = await session.execute(
            select(Truck).where(Truck.id == truck_uuid, Truck.user_id == user_uuid)
        )
        truck = truck_result.scalar_one_or_none()

        if not truck:
            raise HTTPException(status_code=404, detail="Vehicle not found")

        if truck.verification_status != "verified":
            raise HTTPException(status_code=403, detail="Vehicle must be verified")

        now = datetime.utcnow()
        available = post_data.available_date or now.date()

        route = TruckRoute(
            truck_id=truck_uuid,
            user_id=user_uuid,
            truck_number=truck.truck_number,
            truck_type=truck.truck_type,
            capacity=truck.capacity,
            origin_name=post_data.origin.name,
            destination_name=post_data.destination.name,
            origin_location=make_geography_point(
                post_data.origin.lng, post_data.origin.lat
            ),
            destination_location=make_geography_point(
                post_data.destination.lng, post_data.destination.lat
            ),
            origin=post_data.origin.model_dump(),
            destination=post_data.destination.model_dump(),
            current_location=post_data.currentLocation.model_dump(),
            available_date=available,
            status="available",
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        session.add(route)
        await session.commit()
        await session.refresh(route)

        post_dict = truck_route_to_dict(route)
        await process_smart_match_notifications(str(route.id), post_dict, session)

        return {"success": True, "post": post_dict}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in create_truck_post: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/my-posts/{user_id}")
async def get_my_posts(
    user_id: str,
    status: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Get user's truck posts"""
    try:
        user_uuid = parse_uuid(user_id)
        stmt = select(TruckRoute).where(TruckRoute.user_id == user_uuid)
        now = datetime.utcnow()

        if status == "active":
            stmt = stmt.where(
                TruckRoute.status.in_(("available", "active")),
                TruckRoute.expires_at > now,
            )
        elif status == "expired":
            stmt = stmt.where(
                or_(
                    TruckRoute.status == "expired",
                    TruckRoute.expires_at <= now,
                )
            )

        stmt = stmt.order_by(TruckRoute.created_at.desc()).limit(100)
        result = await session.execute(stmt)
        posts = result.scalars().all()

        response_posts = []
        for post in posts:
            if post.status in ("available", "active") and post.expires_at < now:
                post.status = "expired"
            response_posts.append(truck_route_to_dict(post))

        if any(p["status"] == "expired" for p in response_posts):
            await session.commit()

        return {"success": True, "posts": response_posts}
    except Exception as e:
        logger.error(f"Error in get_my_posts: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reactivate/{post_id}")
async def reactivate_post(
    post_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Reactivate an expired post"""
    try:
        post_uuid = parse_uuid(post_id)
        result = await session.execute(
            select(TruckRoute).where(TruckRoute.id == post_uuid)
        )
        post = result.scalar_one_or_none()

        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        authorize_user_id(str(post.user_id), current_user)

        now = datetime.utcnow()
        post.status = "available"
        post.created_at = now
        post.expires_at = now + timedelta(hours=24)
        await session.commit()
        await session.refresh(post)

        post_dict = truck_route_to_dict(post)
        await process_smart_match_notifications(post_id, post_dict, session)

        return {"success": True, "message": "Post reactivated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in reactivate_post: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/delete/{post_id}")
async def delete_post(
    post_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Delete a truck post"""
    try:
        post_uuid = parse_uuid(post_id)
        existing = await session.execute(
            select(TruckRoute).where(TruckRoute.id == post_uuid)
        )
        post = existing.scalar_one_or_none()
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        authorize_user_id(str(post.user_id), current_user)

        result = await session.execute(
            delete(TruckRoute).where(TruckRoute.id == post_uuid)
        )
        await session.commit()

        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Post not found")

        return {"success": True, "message": "Post deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in delete_post: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
