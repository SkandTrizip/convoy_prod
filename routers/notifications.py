from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import Notification
from db.serializers import notification_to_dict, parse_uuid

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/{user_id}")
async def get_notifications(
    user_id: str,
    session: AsyncSession = Depends(get_session),
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
):
    """Mark notification as read"""
    try:
        await session.execute(
            update(Notification)
            .where(Notification.id == parse_uuid(notification_id))
            .values(read_status=True)
        )
        await session.commit()

        return {"success": True, "message": "Notification marked as read"}
    except Exception as e:
        logger.error(f"Error in mark_notification_read: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
