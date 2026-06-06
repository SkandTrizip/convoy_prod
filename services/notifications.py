from datetime import datetime
from typing import Dict, Optional

import requests
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from db.base import User
from db.serializers import parse_uuid


async def send_expo_push_notification(
    user_id: str,
    title: str,
    body: str,
    data: Dict = None,
    session: Optional[AsyncSession] = None,
):
    """Send push notification via Expo"""
    try:
        if session is None:
            logger.warning(f"No session provided for push notification to user {user_id}")
            return

        result = await session.execute(
            select(User).where(User.id == parse_uuid(user_id))
        )
        user = result.scalar_one_or_none()
        if not user or not user.push_token:
            logger.warning(f"No push token found for user {user_id}")
            return

        response = requests.post(
            "https://exp.host/--/api/v2/push/send",
            json={
                "to": user.push_token,
                "title": title,
                "body": body,
                "data": data or {},
                "sound": "default",
            },
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        logger.info(f"Push notification sent to user {user_id}: {response.status_code}")
    except Exception as e:
        logger.error(f"Error sending push notification: {str(e)}")
