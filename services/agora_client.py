"""Builds the tokens clients need to join an Agora RTC/RTM 1:1 call session.

Agora's SDK never talks to this backend directly - the app id + certificate
live here so raw credentials never ship inside the mobile app, and every join
is scoped to a channel/uid/role (RTC) or account (RTM) with a
server-controlled expiry.
"""

import time
import uuid

from agora_token_builder import RtcTokenBuilder, RtmTokenBuilder
from fastapi import HTTPException

from config import AGORA_APP_CERTIFICATE, AGORA_APP_ID, AGORA_MAX_CALL_SECONDS, AGORA_TOKEN_EXPIRATION_SECONDS

# Agora RTC role constants (agora_token_builder.RtcTokenBuilder.Role_*)
RTC_ROLE_PUBLISHER = 1  # can send and receive audio/video
RTC_ROLE_SUBSCRIBER = 2  # can only receive audio/video

# Agora RTM role constant (agora_token_builder.RtmTokenBuilder.Role_Rtm_User)
RTM_ROLE_USER = 1


def require_configured() -> None:
    if not AGORA_APP_ID or not AGORA_APP_CERTIFICATE:
        raise HTTPException(status_code=503, detail="Video calls are not configured")


def uid_for_user(user_id: uuid.UUID) -> int:
    """Agora RTC uids are 32-bit unsigned ints; our user_id is a UUID, so
    derive a stable numeric uid from it rather than requiring clients to
    invent one."""
    return user_id.int & 0x7FFFFFFF


def max_call_seconds() -> int:
    """Hard cap on call duration. Callers use this to compute a per-call
    `not_after_ts` (`started_at + this`) so a token can never authenticate
    past that point, however late a guest joins or however long
    AGORA_TOKEN_EXPIRATION_SECONDS is."""
    return AGORA_MAX_CALL_SECONDS


def build_rtc_token(*, channel_name: str, uid: int, is_host: bool, not_after_ts: int | None = None) -> tuple[str, int]:
    """Returns (token, expires_at_unix_ts).

    `not_after_ts`, when given, caps the token's expiry at that absolute unix
    timestamp even if the normal TTL would run longer - this is what enforces
    a per-call max duration server-side.
    """
    require_configured()
    expire_ts = int(time.time()) + AGORA_TOKEN_EXPIRATION_SECONDS
    if not_after_ts is not None:
        expire_ts = min(expire_ts, not_after_ts)
    role = RTC_ROLE_PUBLISHER if is_host else RTC_ROLE_SUBSCRIBER
    token = RtcTokenBuilder.buildTokenWithUid(AGORA_APP_ID, AGORA_APP_CERTIFICATE, channel_name, uid, role, expire_ts)
    return token, expire_ts


def build_rtm_token(*, account: str, not_after_ts: int | None = None) -> tuple[str, int]:
    """Returns (token, expires_at_unix_ts). `account` is the RTM user id (string)."""
    require_configured()
    expire_ts = int(time.time()) + AGORA_TOKEN_EXPIRATION_SECONDS
    if not_after_ts is not None:
        expire_ts = min(expire_ts, not_after_ts)
    token = RtmTokenBuilder.buildToken(AGORA_APP_ID, AGORA_APP_CERTIFICATE, account, RTM_ROLE_USER, expire_ts)
    return token, expire_ts
