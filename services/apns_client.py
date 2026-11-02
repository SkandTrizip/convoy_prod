"""Apple VoIP push (APNs over HTTP/2, token auth) for the iPhone's incoming-call screen.

A VoIP push is the only way to make a closed or locked iPhone ring like a phone call: it wakes the
app, which must put up the system call screen at once. Never throws: the result says what happened so
the caller can fall back to a normal notification.
"""

import asyncio
import time
from dataclasses import dataclass
from enum import Enum

import httpx
import jwt

from config import APNS_BUNDLE_ID, APNS_KEY_ID, APNS_KEY_P8, APNS_KEY_PATH, APNS_TEAM_ID, logger

PRODUCTION_HOST = "https://api.push.apple.com"
SANDBOX_HOST = "https://api.sandbox.push.apple.com"
# Apple wants the provider token refreshed between every 20 and 60 minutes.
_TOKEN_MAX_AGE_SECONDS = 50 * 60
# Reasons that mean this device token is permanently useless (not a passing problem).
_DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


class ApnsOutcome(str, Enum):
    SENT = "sent"
    INVALID_TOKEN = "invalid_token"  # drop the stored VoIP token
    FAILED = "failed"  # passing problem: fall back to a normal notification
    NOT_CONFIGURED = "not_configured"


@dataclass
class ApnsResult:
    outcome: ApnsOutcome
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == ApnsOutcome.SENT


def _load_key() -> str | None:
    if APNS_KEY_P8.strip():
        return APNS_KEY_P8.replace("\\n", "\n")
    if APNS_KEY_PATH:
        try:
            with open(APNS_KEY_PATH, encoding="utf-8") as handle:
                return handle.read()
        except OSError as e:
            logger.error("Cannot read APNS_KEY_PATH: %s", e)
    return None


def is_configured() -> bool:
    return bool(APNS_KEY_ID and APNS_TEAM_ID and APNS_BUNDLE_ID and _load_key())


class _Provider:
    """Holds the signed provider token and the HTTP/2 client."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_made_at = 0.0
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    def reset(self, client: httpx.AsyncClient | None = None) -> None:
        """For tests (and key rotation): forget the token, optionally swap the client."""
        self._token = None
        self._token_made_at = 0.0
        self._client = client

    def _client_for_use(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(8.0))
        return self._client

    def token(self, *, force_new: bool = False) -> str:
        now = time.time()
        if force_new or self._token is None or now - self._token_made_at > _TOKEN_MAX_AGE_SECONDS:
            key = _load_key()
            if not key:
                raise RuntimeError("APNs key missing")
            self._token = jwt.encode(
                {"iss": APNS_TEAM_ID, "iat": int(now)},
                key,
                algorithm="ES256",
                headers={"kid": APNS_KEY_ID},
            )
            self._token_made_at = now
        return self._token

    async def post(self, url: str, headers: dict, body: dict) -> httpx.Response:
        return await self._client_for_use().post(url, headers=headers, json=body)


provider = _Provider()


async def send_voip(
    device_token: str, payload: dict, *, sandbox: bool, ttl_seconds: int = 45, bundle_id: str | None = None
) -> ApnsResult:
    """One VoIP push. It expires after [ttl_seconds] (a ring that nobody can still answer must not
    ring later). A refused provider token is renewed and the push retried once."""
    if not is_configured():
        return ApnsResult(ApnsOutcome.NOT_CONFIGURED)
    url = f"{SANDBOX_HOST if sandbox else PRODUCTION_HOST}/3/device/{device_token}"
    topic = f"{bundle_id or APNS_BUNDLE_ID}.voip"

    try:
        for attempt in (1, 2):
            headers = {
                "authorization": f"bearer {provider.token(force_new=attempt == 2)}",
                "apns-topic": topic,
                "apns-push-type": "voip",
                "apns-priority": "10",
                "apns-expiration": str(int(time.time()) + ttl_seconds),
            }
            response = await provider.post(url, headers, payload)
            if response.status_code == 200:
                return ApnsResult(ApnsOutcome.SENT)

            reason = ""
            try:
                reason = str(response.json().get("reason", ""))
            except Exception:  # noqa: BLE001
                pass
            if response.status_code == 403 and reason in ("ExpiredProviderToken", "InvalidProviderToken") and attempt == 1:
                continue
            if response.status_code == 410 or reason in _DEAD_TOKEN_REASONS:
                return ApnsResult(ApnsOutcome.INVALID_TOKEN, reason or str(response.status_code))
            logger.error("APNs refused a VoIP push: %s %s", response.status_code, reason)
            return ApnsResult(ApnsOutcome.FAILED, reason or str(response.status_code))
    except Exception as e:  # noqa: BLE001
        logger.error("APNs VoIP push failed: %s", e)
        return ApnsResult(ApnsOutcome.FAILED, str(e))
    return ApnsResult(ApnsOutcome.FAILED, "unreachable")
