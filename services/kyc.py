import re
import uuid
from typing import Any

import requests

from config import (
    CASHFREE_CLIENT_ID,
    CASHFREE_CLIENT_SECRET,
    CASHFREE_ENVIRONMENT,
    logger,
)

CASHFREE_SANDBOX_URL = "https://sandbox.cashfree.com/verification"
CASHFREE_PROD_URL = "https://api.cashfree.com/verification"

# Sandbox test OTP (Cashfree docs)
CASHFREE_SANDBOX_OTP = "111000"


def _cashfree_configured() -> bool:
    return bool(
        CASHFREE_CLIENT_ID
        and CASHFREE_CLIENT_SECRET
        and not CASHFREE_CLIENT_ID.startswith("your_")
        and not CASHFREE_CLIENT_SECRET.startswith("your_")
    )


def _cashfree_base_url() -> str:
    env = (CASHFREE_ENVIRONMENT or "production").lower()
    if env in ("sandbox", "test", "development"):
        return CASHFREE_SANDBOX_URL
    return CASHFREE_PROD_URL


def _cashfree_headers() -> dict[str, str]:
    return {
        "x-client-id": CASHFREE_CLIENT_ID or "",
        "x-client-secret": CASHFREE_CLIENT_SECRET or "",
        "Content-Type": "application/json",
    }


def normalize_aadhaar(aadhaar: str) -> str:
    digits = re.sub(r"\D", "", aadhaar)
    if len(digits) != 12:
        raise ValueError("Aadhaar number must be 12 digits")
    return digits


def _parse_cashfree_error(response: requests.Response) -> str:
    try:
        body = response.json()
        return body.get("message") or body.get("code") or response.text
    except Exception:
        return response.text or f"Cashfree API error ({response.status_code})"


def send_aadhaar_otp(aadhaar_number: str) -> dict[str, Any]:
    """Request OTP on the mobile linked to the Aadhaar number."""
    aadhaar = normalize_aadhaar(aadhaar_number)

    if not _cashfree_configured():
        logger.warning("Cashfree not configured — using dev mock Aadhaar OTP flow")
        return {
            "success": True,
            "ref_id": f"dev-{uuid.uuid4().hex[:8]}",
            "message": "OTP sent (dev mode). Use 111000 to verify.",
            "dev_mode": True,
        }

    url = f"{_cashfree_base_url()}/offline-aadhaar/otp"
    try:
        response = requests.post(
            url,
            headers=_cashfree_headers(),
            json={"aadhaar_number": aadhaar},
            timeout=30,
        )
        data = response.json()

        if response.status_code >= 400:
            logger.error("Cashfree send OTP failed: %s", _parse_cashfree_error(response))
            return {
                "success": False,
                "message": _parse_cashfree_error(response),
                "status": data.get("status"),
            }

        status = (data.get("status") or "").upper()
        if status == "INVALID":
            return {
                "success": False,
                "message": data.get("message") or "Invalid Aadhaar number",
                "status": status,
            }

        if status != "SUCCESS":
            return {
                "success": False,
                "message": data.get("message") or "Failed to send Aadhaar OTP",
                "status": status,
            }

        ref_id = str(data.get("ref_id", ""))
        message = data.get("message") or "OTP sent to Aadhaar-linked mobile"
        logger.info("Cashfree Aadhaar OTP sent, ref_id=%s", ref_id)
        return {
            "success": True,
            "ref_id": ref_id,
            "message": message,
            "aadhaar_linked": "not linked" not in message.lower(),
        }
    except requests.RequestException as e:
        logger.error("Cashfree send OTP request error: %s", e)
        return {"success": False, "message": "Unable to reach KYC verification service"}


def verify_aadhaar_otp(ref_id: str, otp: str) -> dict[str, Any]:
    """Verify Aadhaar OTP and return UIDAI profile data on success."""
    otp = otp.strip()
    if not otp:
        return {"success": False, "message": "OTP is required"}

    if not _cashfree_configured():
        if otp == CASHFREE_SANDBOX_OTP:
            return {
                "success": True,
                "status": "VALID",
                "verified_data": {
                    "name": "Dev Mode User",
                    "dob": "01-01-1990",
                    "address": "Dev address",
                    "gender": "M",
                    "ref_id": ref_id,
                    "dev_mode": True,
                },
            }
        return {"success": False, "message": "Invalid OTP (dev mode: use 111000)"}

    url = f"{_cashfree_base_url()}/offline-aadhaar/verify"
    try:
        response = requests.post(
            url,
            headers=_cashfree_headers(),
            json={"ref_id": str(ref_id), "otp": otp},
            timeout=30,
        )
        data = response.json()

        if response.status_code >= 400:
            logger.error("Cashfree verify OTP failed: %s", _parse_cashfree_error(response))
            return {
                "success": False,
                "message": _parse_cashfree_error(response),
            }

        status = (data.get("status") or "").upper()
        if status != "VALID":
            return {
                "success": False,
                "message": data.get("message") or "Aadhaar verification failed",
                "status": status,
            }

        verified_data = {
            "name": data.get("name"),
            "dob": data.get("dob"),
            "address": data.get("address"),
            "gender": data.get("gender"),
            "year_of_birth": data.get("year_of_birth"),
            "care_of": data.get("care_of"),
            "split_address": data.get("split_address"),
            "ref_id": data.get("ref_id") or ref_id,
            "cashfree_status": status,
        }
        logger.info("Cashfree Aadhaar verified for ref_id=%s", ref_id)
        return {
            "success": True,
            "status": status,
            "verified_data": verified_data,
        }
    except requests.RequestException as e:
        logger.error("Cashfree verify OTP request error: %s", e)
        return {"success": False, "message": "Unable to reach KYC verification service"}


def verify_kyc_with_cashfree(
    data: dict[str, Any],
    images: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Manual KYC with document images — requires Smart OCR (not yet integrated)."""
    del data, images
    if not _cashfree_configured():
        logger.warning("Cashfree API not configured. Manual KYC requires admin review.")
        return {"status": "under_review", "mock": True}

    logger.warning("Cashfree Smart OCR not integrated. Sending manual KYC for admin review.")
    return {"status": "under_review", "message": "Document verification pending admin review"}
