from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import POST_EXPIRE_HOURS, logger
from database import get_session
from db.base import Truck, TruckRoute, User, new_uuid
from db.serializers import parse_uuid, truck_route_to_dict
from middleware.auth import authorize_user_id, get_current_user, require_path_user
from models import CreateTruckPostRequest, EditTruckPostRequest
# Smart-match notifications are disabled pending multi-destination support in services/matching.py
# (deferred to a later phase — see services/matching.py::process_smart_match_notifications).
from services.activity import record_post_activity
from services.destinations import (
    create_destinations,
    get_destinations_for_route,
    get_destinations_for_routes,
    replace_destinations,
)
from services.post_expiry import (
    ACTIVE_POST_STATUSES,
    DEFAULT_ACTIVE_STATUS,
    EXPIRED_STATUS,
    apply_post_reactivation,
    expire_overdue_posts,
    is_post_expired,
    post_expires_at,
)
from services.spatial import make_geography_point

router = APIRouter(prefix="/posts", tags=["posts"])


@router.post("/create/{user_id}")
async def create_truck_post(
    user_id: str,
    post_data: CreateTruckPostRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Create a truck availability post (auto-expires after 24 hours)."""
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
            id=new_uuid(),
            truck_id=truck_uuid,
            user_id=user_uuid,
            truck_number=truck.truck_number,
            truck_type=truck.truck_type,
            capacity=truck.capacity,
            contact_name=post_data.contactName or user.name,
            contact_number=post_data.contactNumber or user.mobile,
            origin_name=post_data.origin.name,
            origin_location=make_geography_point(
                post_data.origin.lng, post_data.origin.lat
            ),
            origin=post_data.origin.model_dump(),
            current_location=post_data.currentLocation.model_dump(),
            available_date=available,
            status=DEFAULT_ACTIVE_STATUS,
            created_at=now,
            expires_at=post_expires_at(now),
        )
        destinations = await create_destinations(session, route.id, post_data.destinations)
        session.add(route)
        await session.commit()
        await session.refresh(route)

        await record_post_activity(session, user_uuid, route.id)

        post_dict = truck_route_to_dict(route, destinations)
        # Smart-match notifications disabled this phase — see import comment above.

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
    """Get user's truck posts (active or expired)."""
    try:
        await expire_overdue_posts(session)

        user_uuid = parse_uuid(user_id)
        stmt = select(TruckRoute).where(TruckRoute.user_id == user_uuid)
        now = datetime.utcnow()

        if status == "active":
            stmt = stmt.where(
                TruckRoute.status.in_(ACTIVE_POST_STATUSES),
                TruckRoute.expires_at > now,
            )
        elif status == "expired":
            stmt = stmt.where(
                or_(
                    TruckRoute.status == EXPIRED_STATUS,
                    TruckRoute.expires_at <= now,
                )
            )

        stmt = stmt.order_by(TruckRoute.created_at.desc()).limit(100)
        result = await session.execute(stmt)
        posts = result.scalars().all()

        destinations_by_route = await get_destinations_for_routes(
            session, [post.id for post in posts]
        )

        return {
            "success": True,
            "posts": [
                truck_route_to_dict(post, destinations_by_route.get(str(post.id), []))
                for post in posts
            ],
        }
    except Exception as e:
        logger.error(f"Error in get_my_posts: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reactivate/{post_id}")
async def reactivate_post(
    post_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Reactivate an expired post for another 24 hours."""
    try:
        await expire_overdue_posts(session)

        post_uuid = parse_uuid(post_id)
        result = await session.execute(
            select(TruckRoute).where(TruckRoute.id == post_uuid)
        )
        post = result.scalar_one_or_none()

        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        authorize_user_id(str(post.user_id), current_user)

        if not is_post_expired(post):
            raise HTTPException(
                status_code=400,
                detail="Post is still active. Reactivation is only for expired posts.",
            )

        apply_post_reactivation(post)
        await session.commit()
        await session.refresh(post)

        destinations = await get_destinations_for_route(session, post.id)
        post_dict = truck_route_to_dict(post, destinations)
        # Smart-match notifications disabled this phase — see import comment above.

        return {
            "success": True,
            "message": f"Post reactivated for {POST_EXPIRE_HOURS} hours",
            "post": post_dict,
        }
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


@router.put("/edit/{post_id}")
async def edit_post(
    post_id: str,
    edit_data: EditTruckPostRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Edit a truck post (vehicle, destination, current location) and reactivate/extend expiry."""
    try:
        post_uuid = parse_uuid(post_id)
        result = await session.execute(
            select(TruckRoute).where(TruckRoute.id == post_uuid)
        )
        post = result.scalar_one_or_none()
        
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")
            
        authorize_user_id(str(post.user_id), current_user)
        
        # If editing vehicle
        if edit_data.vehicleId:
            truck_uuid = parse_uuid(edit_data.vehicleId)
            truck_result = await session.execute(
                select(Truck).where(Truck.id == truck_uuid, Truck.user_id == post.user_id)
            )
            truck = truck_result.scalar_one_or_none()
            if not truck:
                raise HTTPException(status_code=404, detail="Vehicle not found")
            if truck.verification_status != "verified":
                raise HTTPException(status_code=403, detail="Vehicle must be verified")
                
            post.truck_id = truck_uuid
            post.truck_number = truck.truck_number
            post.truck_type = truck.truck_type
            post.capacity = truck.capacity
            
        # If editing destinations (full replace)
        if edit_data.destinations:
            await replace_destinations(session, post.id, edit_data.destinations)

        # If editing current location
        if edit_data.currentLocation:
            post.current_location = edit_data.currentLocation.model_dump()

        # If editing contact info
        if edit_data.contactName:
            post.contact_name = edit_data.contactName
        if edit_data.contactNumber:
            post.contact_number = edit_data.contactNumber

        # Reset expiry and ensure status is active
        now = datetime.utcnow()
        post.status = DEFAULT_ACTIVE_STATUS
        post.expires_at = post_expires_at(now)

        await session.commit()
        await session.refresh(post)

        destinations = await get_destinations_for_route(session, post.id)
        post_dict = truck_route_to_dict(post, destinations)
        # Smart-match notifications disabled this phase — see import comment above.

        return {
            "success": True,
            "message": "Post updated successfully",
            "post": post_dict,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in edit_post: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
