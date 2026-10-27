from typing import Any

import requests

from config import GOOGLE_PLACES_API_KEY, logger

AUTocomplete_URL = "https://maps.googleapis.com/maps/api/place/autocomplete/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


def _parse_address_components(components: list[dict]) -> dict[str, str | None]:
    pincode = city = state = None
    for component in components:
        types = component.get("types", [])
        if "postal_code" in types:
            pincode = component.get("long_name")
        if "locality" in types:
            city = component.get("long_name")
        elif not city and "administrative_area_level_2" in types:
            city = component.get("long_name")
        if "administrative_area_level_1" in types:
            state = component.get("long_name")
    return {"pincode": pincode, "city": city, "state": state}


def fetch_place_autocomplete(query: str, limit: int = 5) -> list[dict[str, Any]]:
    if not GOOGLE_PLACES_API_KEY:
        return []

    response = requests.get(
        AUTocomplete_URL,
        params={
            "input": query,
            "key": GOOGLE_PLACES_API_KEY,
            "components": "country:in",
        },
        timeout=5,
    )
    if response.status_code != 200:
        logger.error(f"Google autocomplete error: {response.text}")
        return []

    return response.json().get("predictions", [])[:limit]


def fetch_place_details(place_id: str) -> dict[str, Any] | None:
    if not GOOGLE_PLACES_API_KEY:
        return None

    response = requests.get(
        DETAILS_URL,
        params={
            "place_id": place_id,
            "key": GOOGLE_PLACES_API_KEY,
            "fields": "geometry,address_components,formatted_address",
        },
        timeout=5,
    )
    if response.status_code != 200:
        logger.error(f"Google place details error: {response.text}")
        return None

    result = response.json().get("result")
    if not result:
        return None

    geometry = result.get("geometry", {}).get("location", {})
    address = _parse_address_components(result.get("address_components", []))

    return {
        "name": result.get("formatted_address", ""),
        "lat": geometry.get("lat", 0.0),
        "lng": geometry.get("lng", 0.0),
        "pincode": address["pincode"],
        "city": address["city"],
        "state": address["state"],
        "google_place_id": place_id,
        "source": "google",
    }


def fetch_reverse_geocode(lat: float, lng: float) -> dict[str, str | None] | None:
    """City/state for a GPS point. Synchronous like the rest of this module —
    call via asyncio.to_thread from async code."""
    if not GOOGLE_PLACES_API_KEY:
        return None

    response = requests.get(
        GEOCODE_URL,
        params={
            "latlng": f"{lat},{lng}",
            "key": GOOGLE_PLACES_API_KEY,
            "result_type": "locality|administrative_area_level_2|administrative_area_level_1",
        },
        timeout=5,
    )
    if response.status_code != 200:
        logger.error(f"Google reverse geocode error: {response.text}")
        return None

    payload = response.json()
    results = payload.get("results") or []
    if not results:
        if payload.get("status") not in ("OK", "ZERO_RESULTS"):
            logger.error(f"Google reverse geocode error: {payload.get('status')} {payload.get('error_message')}")
        return None

    address = _parse_address_components(results[0].get("address_components", []))
    return {"city": address["city"], "state": address["state"]}
