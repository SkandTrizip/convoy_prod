"""Canonical FCM send path — single Firebase app instance shared by the one-off
event-driven sends (services/notifications.py) and the campaign engine's batch sends."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import firebase_admin
from firebase_admin import credentials, exceptions, messaging

from config import FIREBASE_CREDENTIALS_PATH, logger

_firebase_app = None

# FCM allows at most 500 messages per send_each call.
_BATCH_SIZE = 500

# FCM error types that mean the token is permanently dead — not a transient
# failure, so the owning device should be deactivated rather than retried.
_PERMANENTLY_INVALID_TOKEN_ERRORS = (messaging.UnregisteredError, exceptions.InvalidArgumentError)


@dataclass
class PreparedMessage:
    user_id: str
    device_id: str
    token: str
    title: str
    body: str
    data: Dict = field(default_factory=dict)


@dataclass
class SendResult:
    user_id: str
    device_id: str
    success: bool
    should_deactivate: bool = False


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


def send_single(
    token: str, title: str, body: str, data: Dict = None, device_id: str = ""
) -> SendResult:
    """Send one FCM message. Never raises — failures are reported in the
    returned SendResult, not exceptions."""
    try:
        get_firebase_app()
        message = _to_fcm_message(
            PreparedMessage(
                user_id="", device_id=device_id, token=token, title=title, body=body, data=data or {}
            )
        )
        response = messaging.send(message)
        logger.info(f"Push notification sent: {response}")
        return SendResult(user_id="", device_id=device_id, success=True)
    except _PERMANENTLY_INVALID_TOKEN_ERRORS as e:
        logger.warning(f"FCM token permanently invalid for device {device_id}: {e}")
        return SendResult(user_id="", device_id=device_id, success=False, should_deactivate=True)
    except Exception as e:
        logger.error(f"Error sending push notification: {str(e)}")
        return SendResult(user_id="", device_id=device_id, success=False)


def send_batch(messages: List[PreparedMessage]) -> List[SendResult]:
    """Send many FCM messages, chunked to FCM's 500-per-call limit.

    Returns one SendResult per input message (not deduped by user_id) so
    callers can tell which specific device succeeded/failed — a user with
    multiple devices can have some succeed and others fail in the same batch.
    """
    if not messages:
        return []

    get_firebase_app()
    results: List[SendResult] = []

    for i in range(0, len(messages), _BATCH_SIZE):
        chunk = messages[i : i + _BATCH_SIZE]
        try:
            response = messaging.send_each([_to_fcm_message(m) for m in chunk])
        except Exception as e:
            logger.error(f"FCM batch send failed for a chunk of {len(chunk)}: {str(e)}")
            results.extend(
                SendResult(user_id=m.user_id, device_id=m.device_id, success=False) for m in chunk
            )
            continue

        for msg, result in zip(chunk, response.responses):
            if result.success:
                results.append(SendResult(user_id=msg.user_id, device_id=msg.device_id, success=True))
            else:
                should_deactivate = isinstance(result.exception, _PERMANENTLY_INVALID_TOKEN_ERRORS)
                logger.warning(
                    f"FCM send failed for user {msg.user_id} device {msg.device_id}: {result.exception}"
                )
                results.append(
                    SendResult(
                        user_id=msg.user_id,
                        device_id=msg.device_id,
                        success=False,
                        should_deactivate=should_deactivate,
                    )
                )

    delivered = sum(1 for r in results if r.success)
    logger.info(f"Batch push: {delivered}/{len(results)} delivered")
    return results
