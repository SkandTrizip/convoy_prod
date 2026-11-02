"""The pushes around a 1:1 call's ring: "someone is calling you" and "never mind, they hung up".

Who gets what, per device of the person being called:
  - iPhone with a VoIP token (and Apple VoIP push configured): a VoIP push, so the phone shows the
    system call screen even when closed or locked. If Apple refuses it, the normal notification below.
  - Android: a data-only high-priority push. The app turns it into the full-screen call screen.
  - anything else (iPhone without a VoIP token, web): the normal "Incoming 1:1 call" notification.

Best effort throughout: a push problem must never undo or block a call.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from config import CALL_RING_SECONDS, logger
from db.base import CallRequest
from notifications.repositories import device_repository
from notifications.sender import firebase_sender
from services import apns_client


@dataclass
class PushSummary:
    voip: int = 0
    data_only: int = 0
    notification: int = 0
    dead_voip_tokens: list[str] = field(default_factory=list)


def _iso_utc(moment: datetime) -> str:
    # Whole seconds: Apple's date parser on the phone can refuse 6-digit fractions.
    return moment.replace(tzinfo=timezone.utc, microsecond=0).isoformat()


def incoming_payload(request: CallRequest, caller_name: str, ring_ends_at: datetime) -> dict:
    """What the phone needs to draw the call screen. Kept small and free of money details."""
    return {
        "type": "call_incoming",
        "requestId": str(request.id),
        "callerName": caller_name,
        "topic": (request.topic or "")[:80],
        "ringEndsAt": _iso_utc(ring_ends_at),
    }


async def _fcm(token: str, title: str, body: str, data: dict, device_id: str, **options) -> firebase_sender.SendResult:
    # firebase_admin is blocking; keep it off the event loop.
    return await asyncio.to_thread(
        firebase_sender.send_single, token, title, body, data, device_id, **options
    )


async def notify_incoming_call(
    session: AsyncSession, request: CallRequest, caller_name: str, ring_ends_at: datetime
) -> PushSummary:
    summary = PushSummary()
    payload = incoming_payload(request, caller_name, ring_ends_at)
    string_data = {k: str(v) for k, v in payload.items()}
    devices = await device_repository.get_active_call_devices(session, request.requester_id)
    dead_fcm: list[str] = []

    for device in devices:
        try:
            if device.platform == "ios" and device.voip_token and apns_client.is_configured():
                result = await apns_client.send_voip(
                    device.voip_token,
                    # `aps` is required by Apple; the app reads the rest.
                    {"aps": {"alert": f"{caller_name} is calling you"}, **payload},
                    sandbox=device.voip_sandbox,
                    ttl_seconds=CALL_RING_SECONDS,
                )
                if result.ok:
                    summary.voip += 1
                    continue
                if result.outcome == apns_client.ApnsOutcome.INVALID_TOKEN:
                    summary.dead_voip_tokens.append(device.device_id)
                # any failure: carry on to the ordinary notification below

            if device.platform == "android":
                sent = await _fcm(
                    device.fcm_token, "", "", string_data, device.device_id,
                    data_only=True, high_priority=True, ttl_seconds=CALL_RING_SECONDS,
                )
                if sent.success:
                    summary.data_only += 1
            else:
                sent = await _fcm(
                    device.fcm_token,
                    "Incoming 1:1 call",
                    f"{caller_name} is calling you",
                    string_data,
                    device.device_id,
                    ttl_seconds=CALL_RING_SECONDS,
                )
                if sent.success:
                    summary.notification += 1
            if sent.should_deactivate:
                dead_fcm.append(device.device_id)
        except Exception as e:  # noqa: BLE001 - one bad device must not stop the others
            logger.error("Incoming-call push to device %s failed: %s", device.device_id, e)

    try:
        for device_id in summary.dead_voip_tokens:
            await device_repository.clear_voip_token(session, device_id)
        if dead_fcm:
            await device_repository.deactivate_devices(session, dead_fcm)
        if summary.dead_voip_tokens or dead_fcm:
            await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.error("Cleaning up dead push tokens failed: %s", e)
    return summary


async def notify_call_cancelled(session: AsyncSession, request: CallRequest) -> int:
    """The driver hung up while it was still ringing: tell the requester's phones to drop the call
    screen. A silent data push, best effort (the screen also dismisses itself when the ring ends)."""
    sent_count = 0
    data = {"type": "call_cancelled", "requestId": str(request.id)}
    for device in await device_repository.get_active_call_devices(session, request.requester_id):
        try:
            sent = await _fcm(
                device.fcm_token, "", "", data, device.device_id,
                data_only=True, high_priority=True, ttl_seconds=CALL_RING_SECONDS,
            )
            sent_count += 1 if sent.success else 0
        except Exception as e:  # noqa: BLE001
            logger.error("Call-cancelled push to device %s failed: %s", device.device_id, e)
    return sent_count
