import re
from typing import Any

import requests

from config import ULIP_PROXY_API_KEY, ULIP_PROXY_URL, logger


def _ulip_configured() -> bool:
    return bool(ULIP_PROXY_URL and not ULIP_PROXY_URL.startswith("your_"))


def normalize_vehicle_number(vehicle_number: str) -> str:
    clean = re.sub(r"\s+", "", vehicle_number).upper()
    if len(clean) < 6 or len(clean) > 11:
        raise ValueError("Invalid vehicle number format")
    if not re.match(r"^[A-Z0-9]+$", clean):
        raise ValueError("Invalid vehicle number format")
    return clean


def normalize_dl_number(dl_number: str) -> str:
    clean = re.sub(r"\s+", "", dl_number).upper()
    if not clean:
        raise ValueError("DL number is required")
    return clean


def validate_dl_dob(dob: str) -> str:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", dob):
        raise ValueError("Date of birth must be in YYYY-MM-DD format")
    return dob


def _ulip_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if ULIP_PROXY_API_KEY and not ULIP_PROXY_API_KEY.startswith("your_"):
        headers["Authorization"] = f"Bearer {ULIP_PROXY_API_KEY}"
    return headers


def _call_ulip_proxy(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not _ulip_configured():
        logger.warning("ULIP proxy not configured")
        return {
            "verified": False,
            "message": "ULIP verification service not configured",
            "mock": True,
        }

    url = f"{ULIP_PROXY_URL.rstrip('/')}{path}"
    try:
        response = requests.post(
            url,
            json=payload,
            headers=_ulip_headers(),
            timeout=30,
        )
        data = response.json() if response.content else {}

        if response.status_code >= 400:
            message = data.get("message") or data.get("error") or response.text
            logger.error("ULIP proxy error %s: %s", response.status_code, message)
            return {
                "verified": False,
                "message": message,
                "data": data,
                "status_code": response.status_code,
            }

        verified = bool(data.get("verified"))
        logger.info("ULIP %s verified=%s", path, verified)
        return {
            "verified": verified,
            "message": data.get("message"),
            "data": data.get("data") or data,
        }
    except requests.RequestException as e:
        logger.error("ULIP proxy request failed: %s", e)
        return {
            "verified": False,
            "message": "Unable to reach ULIP verification service",
        }


def _dev_vehicle_response(vehicle_number: str) -> dict[str, Any]:
    return {
        "verified": True,
        "message": "Vehicle verified (dev mode)",
        "data": {
            "vehicleNumber": vehicle_number,
            "registrationStatus": "ACTIVE",
            "vehicleClass": "LMV",
            "mock": True,
        },
        "mock": True,
    }


def _dev_dl_response(dl_number: str, dob: str) -> dict[str, Any]:
    return {
        "verified": True,
        "message": "DL verified (dev mode)",
        "data": {
            "dlNumber": dl_number,
            "dob": dob,
            "status": "ACTIVE",
            "mock": True,
        },
        "mock": True,
    }


def verify_vehicle_registration(vehicle_number: str) -> dict[str, Any]:
    """Verify vehicle via ULIP VAHAN proxy (/verify-vehicle)."""
    clean = normalize_vehicle_number(vehicle_number)

    if not _ulip_configured():
        return _dev_vehicle_response(clean)

    result = _call_ulip_proxy("/verify-vehicle", {"vehicleNumber": clean})
    result["vehicleNumber"] = clean
    return result


def verify_driving_license(dl_number: str, dob: str) -> dict[str, Any]:
    """Verify DL via ULIP SARATHI proxy (/verify-dl)."""
    clean_dl = normalize_dl_number(dl_number)
    clean_dob = validate_dl_dob(dob)

    if not _ulip_configured():
        return _dev_dl_response(clean_dl, clean_dob)

    result = _call_ulip_proxy(
        "/verify-dl",
        {"dlNumber": clean_dl, "dob": clean_dob},
    )
    result["dlNumber"] = clean_dl
    result["dob"] = clean_dob
    return result


def to_api_response(result: dict[str, Any]) -> dict[str, Any]:
    """Canonical response shape for clients."""
    return {
        "success": True,
        "verified": bool(result.get("verified")),
        "message": result.get("message"),
        "data": result.get("data"),
        "mock": result.get("mock", False),
    }
