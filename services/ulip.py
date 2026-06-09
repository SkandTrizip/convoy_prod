import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from config import (
    ULIP_BASE_URL,
    ULIP_LOGIN_URL,
    ULIP_PASSWORD,
    ULIP_USERNAME,
    ULIP_VAHAN_API,
    logger,
)

_TOKEN_CACHE: dict[str, Any] = {"token": None, "expires_at": 0.0}
_TOKEN_LOCK = threading.Lock()
_TOKEN_TTL_SECONDS = 10 * 60
_REQUEST_TIMEOUT_SECONDS = 30


def _ulip_configured() -> bool:
    return bool(
        ULIP_USERNAME
        and ULIP_PASSWORD
        and ULIP_BASE_URL
        and not ULIP_USERNAME.startswith("your_")
    )


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


def _login_url() -> str:
    if ULIP_LOGIN_URL:
        return ULIP_LOGIN_URL.rstrip("/")
    return f"{ULIP_BASE_URL.rstrip('/')}/user/login"


def _ulip_api_url(path: str) -> str:
    return f"{ULIP_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def _extract_login_token(data: dict[str, Any]) -> str | None:
    response = data.get("response")
    if isinstance(response, dict):
        token = response.get("id")
        if isinstance(token, str) and token:
            return token
    for key in ("id", "token", "access_token", "accessToken"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _get_ulip_token(*, force_refresh: bool = False) -> str:
    now = time.time()
    with _TOKEN_LOCK:
        if (
            not force_refresh
            and _TOKEN_CACHE["token"]
            and now < _TOKEN_CACHE["expires_at"]
        ):
            return _TOKEN_CACHE["token"]

    response = requests.post(
        _login_url(),
        json={"username": ULIP_USERNAME, "password": ULIP_PASSWORD},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    data = response.json() if response.content else {}

    if response.status_code >= 400:
        message = data.get("message") or response.text
        raise RuntimeError(f"ULIP login failed ({response.status_code}): {message}")

    if str(data.get("error", "")).lower() == "true":
        raise RuntimeError(data.get("message") or "ULIP login failed")

    token = _extract_login_token(data)
    if not token:
        raise RuntimeError("ULIP login returned no token")

    with _TOKEN_LOCK:
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["expires_at"] = now + _TOKEN_TTL_SECONDS
    return token


def _unwrap_ulip_payload(data: dict[str, Any]) -> Any:
    if str(data.get("error", "")).lower() == "true":
        raise ValueError(data.get("message") or "ULIP returned an error")

    response = data.get("response")
    if isinstance(response, list) and response:
        first = response[0]
        if isinstance(first, dict):
            inner = first.get("response")
            if isinstance(inner, str):
                trimmed = inner.strip()
                if trimmed.startswith("{") or trimmed.startswith("["):
                    try:
                        return json.loads(trimmed)
                    except json.JSONDecodeError:
                        return trimmed
                return trimmed
            return inner
    if isinstance(data.get("data"), dict):
        return data["data"]
    return data


def _call_ulip(path: str, body: dict[str, Any]) -> dict[str, Any]:
    token = _get_ulip_token()
    url = _ulip_api_url(path)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    response = requests.post(
        url,
        json=body,
        headers=headers,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )

    if response.status_code == 401:
        token = _get_ulip_token(force_refresh=True)
        headers["Authorization"] = f"Bearer {token}"
        response = requests.post(
            url,
            json=body,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    data = response.json() if response.content else {}
    if response.status_code >= 400:
        message = data.get("message") or data.get("error") or response.text
        raise RuntimeError(f"ULIP {path} failed ({response.status_code}): {message}")

    return data


def _pick_string(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _vehicle_not_found(payload: Any) -> bool:
    if isinstance(payload, str):
        normalized = payload.lower().replace(" ", "")
        return "notfound" in normalized or "detailsnotfound" in normalized
    return False


def _parse_vahan_xml(xml_str: str) -> dict[str, Any]:
    root = ET.fromstring(xml_str.strip())
    record: dict[str, Any] = {}
    for child in root:
        if child.text and child.text.strip():
            record[child.tag] = child.text.strip()
    return record


def _extract_vahan_record(ulip_response: dict[str, Any]) -> dict[str, Any]:
    payload = _unwrap_ulip_payload(ulip_response)

    if _vehicle_not_found(payload):
        return {}

    if isinstance(payload, str):
        if ULIP_VAHAN_API == "VAHAN/01":
            return _parse_vahan_xml(payload)
        raise ValueError("Unexpected VAHAN response format")

    if not isinstance(payload, dict):
        return {}

    for node_key in ("data", "response"):
        node = payload.get(node_key)
        if isinstance(node, list) and node and isinstance(node[0], dict):
            return node[0]
        if isinstance(node, dict):
            return node

    return payload


def _build_vehicle_data(record: dict[str, Any], vehicle_number: str) -> dict[str, Any]:
    status = _pick_string(
        record,
        "rcStatus",
        "rc_status",
        "Status",
        "vehicleStatus",
    )
    vehicle_class = _pick_string(
        record,
        "rcVhClassDesc",
        "rc_vh_class_desc",
        "Vehicle_class_desc",
        "vehicleClass",
    )
    reg_no = _pick_string(
        record,
        "rcRegnNo",
        "rc_regn_no",
        "Regn_no",
        "registration_number",
    ) or vehicle_number

    return {
        "vehicleNumber": reg_no,
        "registrationStatus": status,
        "vehicleClass": vehicle_class,
        "ownerName": _pick_string(
            record, "rcOwnerName", "rc_owner_name", "Owner_name", "owner_name"
        ),
        "fuelType": _pick_string(record, "rcFuelDesc", "rc_fuel_desc", "Fuel_desc"),
        "manufacturer": _pick_string(
            record, "rcMakerDesc", "rc_maker_desc", "Maker_desc", "manufacturer"
        ),
        "model": _pick_string(
            record, "rcMakerModel", "rc_maker_model", "Maker_model", "model"
        ),
        "raw": record,
    }


def _vehicle_is_verified(record: dict[str, Any]) -> bool:
    if not record:
        return False
    status = _pick_string(
        record,
        "rcStatus",
        "rc_status",
        "Status",
        "vehicleStatus",
    )
    if status and status.upper() == "ACTIVE":
        return True
    return bool(
        _pick_string(
            record,
            "rcRegnNo",
            "rc_regn_no",
            "Regn_no",
            "registration_number",
        )
    )


def _extract_sarathi_record(ulip_response: dict[str, Any]) -> dict[str, Any]:
    payload = _unwrap_ulip_payload(ulip_response)
    if not isinstance(payload, dict):
        return {}

    dldet_raw = payload.get("dldetobj")
    if isinstance(dldet_raw, list) and dldet_raw:
        dldet = dldet_raw[0] if isinstance(dldet_raw[0], dict) else {}
    elif isinstance(dldet_raw, dict):
        dldet = dldet_raw
    else:
        dldet = payload

    return dldet if isinstance(dldet, dict) else {}


def _build_dl_data(record: dict[str, Any], dl_number: str, dob: str) -> dict[str, Any]:
    dlobj = record.get("dlobj") if isinstance(record.get("dlobj"), dict) else {}
    bio_obj = record.get("bioObj") if isinstance(record.get("bioObj"), dict) else {}

    status = _pick_string(dlobj, "dlStatus", "dl_status", "status")
    valid_to = _pick_string(dlobj, "dlNtValdtoDt", "dl_nt_valdto_dt", "valid_to")

    return {
        "dlNumber": dl_number,
        "dob": dob,
        "status": status,
        "validTo": valid_to,
        "fullName": _pick_string(bio_obj, "bioFullName", "bio_full_name", "full_name"),
        "rtoName": _pick_string(dlobj, "omRtoFullname", "om_rto_fullname", "rto_name"),
        "raw": record,
    }


def _dl_is_verified(record: dict[str, Any]) -> bool:
    if not record:
        return False

    dlobj = record.get("dlobj") if isinstance(record.get("dlobj"), dict) else record
    status = _pick_string(dlobj, "dlStatus", "dl_status", "status")
    if not status or status.lower() != "active":
        return False

    valid_to = _pick_string(dlobj, "dlNtValdtoDt", "dl_nt_valdto_dt", "valid_to")
    if not valid_to:
        return True

    try:
        return valid_to[:10] >= time.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return True


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
    """Verify vehicle via ULIP VAHAN API."""
    clean = normalize_vehicle_number(vehicle_number)

    if not _ulip_configured():
        return _dev_vehicle_response(clean)

    try:
        ulip_response = _call_ulip(
            ULIP_VAHAN_API,
            {"vehiclenumber": clean},
        )
        record = _extract_vahan_record(ulip_response)
        verified = _vehicle_is_verified(record)
        data = _build_vehicle_data(record, clean) if record else None

        message = "Vehicle verified" if verified else "Vehicle verification failed"
        if not record:
            message = "Vehicle details not found"

        logger.info("ULIP %s verified=%s vehicle=%s", ULIP_VAHAN_API, verified, clean)
        return {
            "verified": verified,
            "message": message,
            "data": data,
            "vehicleNumber": clean,
        }
    except (requests.RequestException, RuntimeError, ValueError) as e:
        logger.error("ULIP vehicle verification failed: %s", e)
        return {
            "verified": False,
            "message": str(e),
            "vehicleNumber": clean,
        }


def verify_driving_license(dl_number: str, dob: str) -> dict[str, Any]:
    """Verify DL via ULIP SARATHI API."""
    clean_dl = normalize_dl_number(dl_number)
    clean_dob = validate_dl_dob(dob)

    if not _ulip_configured():
        return _dev_dl_response(clean_dl, clean_dob)

    try:
        ulip_response = _call_ulip(
            "SARATHI/01",
            {"dlnumber": clean_dl, "dob": clean_dob},
        )
        record = _extract_sarathi_record(ulip_response)
        verified = _dl_is_verified(record)
        data = _build_dl_data(record, clean_dl, clean_dob) if record else None

        message = "Driving license verified" if verified else "Driving license verification failed"
        if not record:
            message = "Driving license details not found"

        logger.info("ULIP SARATHI/01 verified=%s dl=%s", verified, clean_dl)
        return {
            "verified": verified,
            "message": message,
            "data": data,
            "dlNumber": clean_dl,
            "dob": clean_dob,
        }
    except (requests.RequestException, RuntimeError, ValueError) as e:
        logger.error("ULIP DL verification failed: %s", e)
        return {
            "verified": False,
            "message": str(e),
            "dlNumber": clean_dl,
            "dob": clean_dob,
        }


def to_api_response(result: dict[str, Any]) -> dict[str, Any]:
    """Canonical response shape for clients."""
    return {
        "success": True,
        "verified": bool(result.get("verified")),
        "message": result.get("message"),
        "data": result.get("data"),
        "mock": result.get("mock", False),
    }
