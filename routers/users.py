from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import User
from db.serializers import parse_uuid, user_to_dict
from models import PushTokenRequest, UserProfile

router = APIRouter(prefix="/user", tags=["user"])


@router.get("/profile/{user_id}")
async def get_user_profile(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get user profile"""
    try:
        result = await session.execute(
            select(User).where(User.id == parse_uuid(user_id))
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        return {"success": True, "user": user_to_dict(user)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_user_profile: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/profile/{user_id}")
async def update_user_profile(
    user_id: str,
    profile: UserProfile,
    session: AsyncSession = Depends(get_session),
):
    """Update user profile"""
    try:
        update_data = {}
        if profile.name:
            update_data["name"] = profile.name
        if profile.profilePhoto:
            update_data["profile_photo"] = profile.profilePhoto

        if not update_data:
            result = await session.execute(
                select(User).where(User.id == parse_uuid(user_id))
            )
            if not result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="User not found")
            return {"success": True, "message": "Profile updated successfully"}

        result = await session.execute(
            update(User)
            .where(User.id == parse_uuid(user_id))
            .values(**update_data)
        )

        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")

        await session.commit()
        return {"success": True, "message": "Profile updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in update_user_profile: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/push-token/{user_id}")
async def update_push_token(
    user_id: str,
    data: PushTokenRequest,
    session: AsyncSession = Depends(get_session),
):
    """Update user's push notification token"""
    try:
        push_token = data.pushToken
        await session.execute(
            update(User)
            .where(User.id == parse_uuid(user_id))
            .values(push_token=push_token)
        )
        await session.commit()

        return {"success": True, "message": "Push token updated"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating push token: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
