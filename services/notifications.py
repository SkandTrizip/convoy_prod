from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from db.base import User
from db.serializers import parse_uuid
from notifications.sender.firebase_sender import send_single


async def send_push_notification(
    user_id: str,
    title: str,
    body: str,
    data: Dict = None,
    session: Optional[AsyncSession] = None,
):
    """Send a one-off push notification via FCM (event-driven — KYC approval,
    smart match, etc). For scheduled campaign sends to many users, see
    notifications/sender/firebase_sender.py:send_batch instead."""
    try:
        if session is None:
            logger.warning(f"No session provided for push notification to user {user_id}")
            return

        result = await session.execute(select(User).where(User.id == parse_uuid(user_id)))
        user = result.scalar_one_or_none()
        if not user or not user.push_token:
            logger.warning(f"No push token found for user {user_id}")
            return

        send_single(user.push_token, title, body, data)
    except Exception as e:
        logger.error(f"Error sending push notification: {str(e)}")
