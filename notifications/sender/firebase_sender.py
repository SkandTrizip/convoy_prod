"""Canonical FCM send path — single Firebase app instance shared by the one-off
event-driven sends (services/notifications.py) and the campaign engine's batch sends."""
from dataclasses import dataclass, field
from typing import Dict, List

import firebase_admin
from firebase_admin import credentials, messaging

from config import FIREBASE_CREDENTIALS_PATH, logger

_firebase_app = None

# FCM allows at most 500 messages per send_each call.
_BATCH_SIZE = 500


@dataclass
class PreparedMessage:
    user_id: str
    token: str
    title: str
    body: str
    data: Dict = field(default_factory=dict)


def get_firebase_app():
    global _firebase_app
    if _firebase_app is None:
        cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
        _firebase_app = firebase_admin.initialize_app(cred)
    return _firebase_app


def _to_fcm_message(msg: PreparedMessage) -> messaging.Message:
    return messaging.Message(
        notification=messaging.Notification(title=msg.title, body=msg.body),
        data={str(k): str(v) for k, v in msg.data.items()},
        token=msg.token,
    )


def send_single(token: str, title: str, body: str, data: Dict = None) -> bool:
    """Send one FCM message. Returns True on success, False on failure (never raises)."""
    try:
        get_firebase_app()
        message = _to_fcm_message(
            PreparedMessage(user_id="", token=token, title=title, body=body, data=data or {})
        )
        response = messaging.send(message)
        logger.info(f"Push notification sent: {response}")
        return True
    except Exception as e:
        logger.error(f"Error sending push notification: {str(e)}")
        return False


def send_batch(messages: List[PreparedMessage]) -> List[str]:
    """Send many FCM messages, chunked to FCM's 500-per-call limit.

    Returns the user_ids whose message was accepted by FCM, so callers know
    which sends to record as delivered vs. skip.
    """
    if not messages:
        return []

    get_firebase_app()
    sent_user_ids: List[str] = []

    for i in range(0, len(messages), _BATCH_SIZE):
        chunk = messages[i : i + _BATCH_SIZE]
        try:
            response = messaging.send_each([_to_fcm_message(m) for m in chunk])
        except Exception as e:
            logger.error(f"FCM batch send failed for a chunk of {len(chunk)}: {str(e)}")
            continue

        for msg, result in zip(chunk, response.responses):
            if result.success:
                sent_user_ids.append(msg.user_id)
            else:
                logger.warning(
                    f"FCM send failed for user {msg.user_id}: {result.exception}"
                )

    logger.info(f"Batch push: {len(sent_user_ids)}/{len(messages)} delivered")
    return sent_user_ids
