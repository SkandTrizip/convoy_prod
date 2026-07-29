from typing import Dict, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from notifications.repositories import device_repository
from notifications.sender.firebase_sender import send_single


async def send_push_notification(
    user_id: str,
    title: str,
    body: str,
    data: Dict = None,
    session: Optional[AsyncSession] = None,
):
    """Send a one-off push notification via FCM (event-driven — KYC approval,
    smart match, etc), fanned out to every active device the user has
    registered. For scheduled campaign sends to many users, see
    notifications/sender/firebase_sender.py:send_batch instead."""
    try:
        if session is None:
            logger.warning(f"No session provided for push notification to user {user_id}")
            return

        devices = await device_repository.get_active_devices_for_user_ids(session, [UUID(user_id)])
        user_devices = devices.get(user_id, [])
        if not user_devices:
            logger.warning(f"No active devices found for user {user_id}")
            return

        invalid_device_ids = []
        for device in user_devices:
            result = send_single(device.fcm_token, title, body, data, device_id=device.device_id)
            if result.should_deactivate:
                invalid_device_ids.append(device.device_id)

        if invalid_device_ids:
            await device_repository.deactivate_devices(session, invalid_device_ids)
            await session.commit()
    except Exception as e:
        logger.error(f"Error sending push notification: {str(e)}")
