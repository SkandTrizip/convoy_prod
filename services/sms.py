import time
from typing import Any

import requests

from config import (
    VEUP_CAMPAIGN_NAME,
    VEUP_PROCESS_KEY,
    VEUP_SENDER_ID,
    VEUP_TEMPLATE_ID,
    logger,
)

VEUP_TOKEN_URL = "https://token.veup.io/api/v1"
VEUP_MESSAGE_URL = "https://c-api.veup.io/v1/message"

_token_cache: dict[str, Any] = {"api_key": None, "expires_at": 0}


def _veup_configured() -> bool:
    return bool(VEUP_PROCESS_KEY and not VEUP_PROCESS_KEY.startswith("your_"))


def normalize_indian_mobile(mobile: str) -> str:
    digits = "".join(c for c in mobile if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    return digits


def _get_veup_api_key() -> str | None:
    if not _veup_configured():
        return None

    now = time.time()
    if _token_cache["api_key"] and now < _token_cache["expires_at"]:
        return _token_cache["api_key"]

    try:
        response = requests.post(
            VEUP_TOKEN_URL,
            json={"process_key": VEUP_PROCESS_KEY},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        api_key = data.get("apiKey") or data.get("api_key")
        if not api_key:
            logger.error("Veup token response missing apiKey")
            return None

        _token_cache["api_key"] = api_key
        _token_cache["expires_at"] = now + 3600
        return api_key
    except Exception as e:
        logger.error(f"Error fetching Veup API token: {e}")
        return None


def build_otp_message(otp: str) -> str:
    return (
        f"Dear User, Your OTP for Sharkship login is {otp}. "
        "Valid for 10 minutes. Do not share it. Powered by Sharkship."
    )


def send_otp_sms(mobile: str, otp: str) -> bool:
    """Send login OTP SMS via Veup."""
    api_key = _get_veup_api_key()
    if not api_key:
        logger.warning("Veup SMS not sent (missing credentials or token request failed).")
        return False

    number = normalize_indian_mobile(mobile)
    payload = {
        "api_key": VEUP_PROCESS_KEY,
        "campaign_name": VEUP_CAMPAIGN_NAME,
        "to": {"number": number, "is_international": False},
        "delivery": {"type": "single", "channels": ["sms"]},
        "campaign_data": {
            "sms": {
                "text": build_otp_message(otp),
                "sender_id": VEUP_SENDER_ID,
                "template_id": VEUP_TEMPLATE_ID,
            }
        },
    }

    try:
        response = requests.post(
            VEUP_MESSAGE_URL,
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        logger.info(f"OTP SMS sent via Veup to {number}")
        return True
    except Exception as e:
        logger.error(f"Error sending Veup SMS: {e}")
        return False


def is_sms_dev_mode() -> bool:
    return not _veup_configured()
