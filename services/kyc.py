import base64
import binascii
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
CASHFREE_SMART_OCR_PATH = "/bharat-ocr"
CASHFREE_API_VERSION = "2024-12-01"

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


def _cashfree_ocr_headers() -> dict[str, str]:
    return {
        "x-client-id": CASHFREE_CLIENT_ID or "",
        "x-client-secret": CASHFREE_CLIENT_SECRET or "",
        "x-api-version": CASHFREE_API_VERSION,
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


def _sanitize_verification_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "-", value)
    return cleaned[:50] or f"ocr-{uuid.uuid4().hex[:12]}"


def _prepare_image_for_ocr(image: str) -> dict[str, Any]:
    """Accept HTTPS URL or base64 (optionally data-URI) for Smart OCR upload."""
    image = image.strip()
    if not image:
        raise ValueError("Image is required")

    if image.startswith("https://"):
        return {"file_url": image}

    if image.startswith("data:"):
        _, _, payload = image.partition(",")
        if not payload:
            raise ValueError("Invalid data URI image")
        try:
            return {"file_bytes": base64.b64decode(payload, validate=True)}
        except binascii.Error as e:
            raise ValueError("Invalid base64 image data") from e

    try:
        return {"file_bytes": base64.b64decode(image, validate=True)}
    except binascii.Error as e:
        raise ValueError("Image must be a base64 string or HTTPS URL") from e


def _mock_smart_ocr_response(verification_id: str, side: str) -> dict[str, Any]:
    doc_type = "AADHAAR_FRONT" if side == "front" else "AADHAAR_BACK"
    fields: dict[str, Any] = {
        "name": "Dev Mode User",
        "dob": "1990-01-01",
        "gender": "Male",
        "address": "Dev address, India",
        "uid": "123456789012",
    }
    if side == "back":
        fields = {
            "address": "Dev address, India",
            "pincode": "110001",
            "uid": "123456789012",
        }
    return {
        "success": True,
        "verification_id": verification_id,
        "reference_id": 0,
        "status": "VALID",
        "document_type": doc_type,
        "document_fields": fields,
        "quality_checks": {},
        "fraud_checks": {},
        "dev_mode": True,
    }


def run_smart_ocr(
    verification_id: str,
    image: str,
    document_type: str = "AADHAAR",
) -> dict[str, Any]:
    """Upload an ID image to Cashfree Smart OCR (POST /bharat-ocr)."""
    verification_id = _sanitize_verification_id(verification_id)

    if not _cashfree_configured():
        side = "front" if "front" in verification_id else "back"
        logger.warning("Cashfree not configured — using dev mock Smart OCR")
        return _mock_smart_ocr_response(verification_id, side)

    try:
        prepared = _prepare_image_for_ocr(image)
    except ValueError as e:
        return {"success": False, "message": str(e)}

    url = f"{_cashfree_base_url()}{CASHFREE_SMART_OCR_PATH}"
    form_data = {
        "verification_id": verification_id,
        "document_type": document_type,
    }
    files = None
    if prepared.get("file_bytes"):
        files = {"file": ("aadhaar.jpg", prepared["file_bytes"], "image/jpeg")}
    else:
        form_data["file_url"] = prepared["file_url"]

    try:
        response = requests.post(
            url,
            headers=_cashfree_ocr_headers(),
            data=form_data,
            files=files,
            timeout=60,
        )
        data = response.json() if response.content else {}

        if response.status_code >= 400:
            logger.error("Cashfree Smart OCR failed: %s", _parse_cashfree_error(response))
            return {
                "success": False,
                "message": _parse_cashfree_error(response),
                "status": data.get("status"),
            }

        status = (data.get("status") or "").upper()
        return {
            "success": status == "VALID",
            "verification_id": data.get("verification_id") or verification_id,
            "reference_id": data.get("reference_id"),
            "status": status,
            "document_type": data.get("document_type"),
            "document_fields": data.get("document_fields") or {},
            "quality_checks": data.get("quality_checks") or {},
            "fraud_checks": data.get("fraud_checks") or {},
            "qr_details": data.get("qr_details"),
            "message": data.get("message"),
        }
    except requests.RequestException as e:
        logger.error("Cashfree Smart OCR request error: %s", e)
        return {"success": False, "message": "Unable to reach KYC verification service"}


def _ocr_passes_fraud_checks(fraud_checks: dict[str, Any]) -> bool:
    if not fraud_checks:
        return True
    for key in ("is_forged", "is_overwritten", "is_photo_imposed"):
        if fraud_checks.get(key):
            return False
    return True


def _extract_uid(document_fields: dict[str, Any]) -> str | None:
    uid = document_fields.get("uid")
    if uid:
        digits = re.sub(r"\D", "", str(uid))
        return digits if len(digits) == 12 else None
    last4 = document_fields.get("aadhaar_last_four_digit")
    return None if not last4 else None


def verify_aadhaar_with_smart_ocr(
    front_image: str,
    back_image: str,
    user_id: str | None = None,
    expected_aadhaar: str | None = None,
) -> dict[str, Any]:
    """Run Smart OCR on Aadhaar front/back images and validate extracted data."""
    if not front_image or not back_image:
        return {
            "success": False,
            "status": "rejected",
            "message": "Both Aadhaar front and back images are required",
        }

    base_id = _sanitize_verification_id(user_id or uuid.uuid4().hex)
    front = run_smart_ocr(f"{base_id}-front", front_image)
    back = run_smart_ocr(f"{base_id}-back", back_image)

    if not front.get("success"):
        return {
            "success": False,
            "status": "rejected",
            "message": front.get("message") or "Aadhaar front image verification failed",
            "ocr": {"front": front, "back": back},
        }

    if not back.get("success"):
        return {
            "success": False,
            "status": "rejected",
            "message": back.get("message") or "Aadhaar back image verification failed",
            "ocr": {"front": front, "back": back},
        }

    for label, result in (("front", front), ("back", back)):
        if not _ocr_passes_fraud_checks(result.get("fraud_checks") or {}):
            return {
                "success": False,
                "status": "rejected",
                "message": f"Aadhaar {label} image failed fraud checks",
                "ocr": {"front": front, "back": back},
            }

    front_fields = front.get("document_fields") or {}
    back_fields = back.get("document_fields") or {}
    front_uid = _extract_uid(front_fields)
    back_uid = _extract_uid(back_fields)

    if front_uid and back_uid and front_uid != back_uid:
        return {
            "success": False,
            "status": "rejected",
            "message": "Aadhaar number on front and back images do not match",
            "ocr": {"front": front, "back": back},
        }

    if expected_aadhaar:
        try:
            expected = normalize_aadhaar(expected_aadhaar)
        except ValueError as e:
            return {"success": False, "status": "rejected", "message": str(e)}

        matched_uid = front_uid or back_uid
        if matched_uid and matched_uid != expected:
            return {
                "success": False,
                "status": "rejected",
                "message": "Aadhaar number does not match the document",
                "ocr": {"front": front, "back": back},
            }
        if not matched_uid and expected[-4:] not in (
            str(front_fields.get("aadhaar_last_four_digit", "")),
            str(back_fields.get("aadhaar_last_four_digit", "")),
        ):
            logger.warning("Smart OCR could not confirm full Aadhaar number match")

    verified_data = {
        "name": front_fields.get("name"),
        "dob": front_fields.get("dob"),
        "address": front_fields.get("address") or back_fields.get("address"),
        "gender": front_fields.get("gender"),
        "aadhaar_last4": (front_uid or back_uid or "")[-4:] or None,
        "pincode": back_fields.get("pincode"),
        "cashfree_status": "VALID",
        "verification_method": "smart_ocr",
        "front_reference_id": front.get("reference_id"),
        "back_reference_id": back.get("reference_id"),
    }

    logger.info(
        "Cashfree Smart OCR Aadhaar verified for user=%s ref_front=%s ref_back=%s",
        user_id,
        front.get("reference_id"),
        back.get("reference_id"),
    )
    return {
        "success": True,
        "status": "approved",
        "message": "Aadhaar verified via Smart OCR",
        "verified_data": verified_data,
        "ocr": {"front": front, "back": back},
    }


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
            "verification_method": "aadhaar_otp",
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
    user_id: str | None = None,
) -> dict[str, Any]:
    """Verify KYC document images via Cashfree Smart OCR."""
    images = images or {}
    front = images.get("front")
    back = images.get("back")

    if not front and not back:
        if not _cashfree_configured():
            logger.warning("Cashfree API not configured. Manual KYC requires admin review.")
            return {"success": False, "status": "under_review", "mock": True}
        return {
            "success": False,
            "status": "under_review",
            "message": "Document images required for Smart OCR verification",
        }

    expected_aadhaar = data.get("aadhaar") or data.get("aadhaarNumber")
    result = verify_aadhaar_with_smart_ocr(
        front_image=front or "",
        back_image=back or "",
        user_id=user_id,
        expected_aadhaar=str(expected_aadhaar) if expected_aadhaar else None,
    )
    if result.get("success"):
        return result

    if not _cashfree_configured() and result.get("ocr", {}).get("front", {}).get("dev_mode"):
        return result

    status = result.get("status", "under_review")
    if status == "rejected":
        return result
    return {
        **result,
        "status": "under_review",
        "message": result.get("message") or "Document verification pending admin review",
    }
