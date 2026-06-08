from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import Notification, User
from db.serializers import notification_to_dict, parse_uuid
from middleware.auth import authorize_user_id, get_current_user, require_path_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/{user_id}")
async def get_notifications(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Get user's notifications"""
    try:
        result = await session.execute(
            select(Notification)
            .where(Notification.user_id == parse_uuid(user_id))
            .order_by(Notification.created_at.desc())
            .limit(50)
        )
        notifications = result.scalars().all()

        return {
            "success": True,
            "notifications": [notification_to_dict(n) for n in notifications],
        }
    except Exception as e:
        logger.error(f"Error in get_notifications: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mark-read/{notification_id}")
async def mark_notification_read(
    notification_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Mark notification as read"""
    try:
        notification_uuid = parse_uuid(notification_id)
        existing = await session.execute(
            select(Notification).where(Notification.id == notification_uuid)
        )
        notification = existing.scalar_one_or_none()
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")

        authorize_user_id(str(notification.user_id), current_user)

        await session.execute(
            update(Notification)
            .where(Notification.id == notification_uuid)
            .values(read_status=True)
        )
        await session.commit()

        return {"success": True, "message": "Notification marked as read"}
    except Exception as e:
        logger.error(f"Error in mark_notification_read: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
