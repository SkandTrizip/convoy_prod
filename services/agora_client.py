"""Builds the tokens clients need to join a paid 1:1 call (Agora RTC audio/video,
Agora RTM chat).

Agora's SDK never talks to this backend directly - the app id + certificate live
here so raw credentials never ship inside the mobile app, and every join is scoped
to a channel/uid (RTC) or user (RTM) with a server-controlled expiry.

RTC tokens use the `agora_token_builder` package (legacy "006" format, which RTC
accepts). RTM 2.x REJECTS that format ("Invalid token", verified on a device in the
Stage 0 spike), so chat tokens need Agora's AccessToken2 ("007") builder, vendored
at services/agora_token2/. Until that folder exists, `build_rtm_token` returns None
and the call API reports `chatAvailable: false` instead of handing out a token that
can never work.
"""

import time
import uuid

from agora_token_builder import RtcTokenBuilder
from fastapi import HTTPException

from config import AGORA_APP_CERTIFICATE, AGORA_APP_ID, AGORA_TOKEN_EXPIRATION_SECONDS

try:  # vendored copy of AgoraIO/Tools DynamicKey/AgoraDynamicKey/python3/src
    from services.agora_token2.RtmTokenBuilder2 import RtmTokenBuilder as _RtmTokenBuilderV2
except ImportError:
    _RtmTokenBuilderV2 = None

# Agora RTC role constants (agora_token_builder.RtcTokenBuilder.Role_*)
RTC_ROLE_PUBLISHER = 1  # can send and receive audio/video
RTC_ROLE_SUBSCRIBER = 2  # can only receive audio/video


def require_configured() -> None:
    if not AGORA_APP_ID or not AGORA_APP_CERTIFICATE:
        raise HTTPException(status_code=503, detail="Video calls are not configured")


def chat_available() -> bool:
    return _RtmTokenBuilderV2 is not None


def uid_for_user(user_id: uuid.UUID) -> int:
    """Agora RTC uids are 32-bit unsigned ints; our user_id is a UUID, so derive a
    stable numeric uid from it rather than requiring clients to invent one."""
    return user_id.int & 0x7FFFFFFF


def build_rtc_token(*, channel_name: str, uid: int, not_after_ts: int) -> tuple[str, int]:
    """Returns (token, expires_at_unix_ts). The token can never outlive
    `not_after_ts` (the call's hard end), however long the normal TTL is: Agora
    disconnects a client when its token expires, which is what enforces the
    chosen call duration even if the app misbehaves.

    Both sides get publisher rights: the legacy format can't say "audio only", so
    the requester not sending video is enforced by their app for now (the 007
    builder can enforce it here)."""
    require_configured()
    expire_ts = min(int(time.time()) + AGORA_TOKEN_EXPIRATION_SECONDS, not_after_ts)
    token = RtcTokenBuilder.buildTokenWithUid(
        AGORA_APP_ID, AGORA_APP_CERTIFICATE, channel_name, uid, RTC_ROLE_PUBLISHER, expire_ts
    )
    return token, expire_ts


def build_rtm_token(*, account: str, not_after_ts: int) -> tuple[str, int] | None:
    """Returns (token, expires_at_unix_ts), or None while the 007 builder isn't
    vendored. `account` is the RTM user id (the app logs in with the same string)."""
    if _RtmTokenBuilderV2 is None:
        return None
    require_configured()
    expire_ts = min(int(time.time()) + AGORA_TOKEN_EXPIRATION_SECONDS, not_after_ts)
    ttl = max(expire_ts - int(time.time()), 1)
    return _RtmTokenBuilderV2.build_token(AGORA_APP_ID, AGORA_APP_CERTIFICATE, account, ttl), expire_ts
