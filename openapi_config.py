import os

from config import PORT

API_TITLE = "Convoy API"
API_VERSION = "1.0.0"
API_DESCRIPTION = """
Convoy is a truck discovery and availability marketplace API.

## Authentication
1. **POST /api/auth/send-otp** then **POST /api/auth/verify-otp** to log in.
2. Use the returned **`accessToken`** as a Bearer JWT on all other endpoints:
   `Authorization: Bearer <accessToken>`
3. Path `user_id` must match the logged-in user (from the token).

## Interactive docs
- **Swagger UI:** `/docs`
- **ReDoc:** `/redoc`
- **OpenAPI JSON:** `/openapi.json`

## Base path
All routes are served under **`/api`**.
"""

OPENAPI_TAGS = [
    {
        "name": "auth",
        "description": "Mobile OTP login and registration.",
    },
    {
        "name": "user",
        "description": "User profile and Expo push notification token.",
    },
    {
        "name": "contacts",
        "description": "Hashed phone contact sync and mutual-connection matching.",
    },
    {
        "name": "locations",
        "description": "Location autosuggest (local DB + Google Places).",
    },
    {
        "name": "kyc",
        "description": "Aadhaar OTP and KYC submission workflows.",
    },
    {
        "name": "vehicle",
        "description": "Add and list verified trucks for a user.",
    },
    {
        "name": "verification",
        "description": "VAHAN vehicle and SARATHI DL verification via ULIP API.",
    },
    {
        "name": "posts",
        "description": "Create, list, reactivate, and delete truck availability posts.",
    },
    {
        "name": "search",
        "description": "Spatial truck search, demand tracking, and call logging.",
    },
    {
        "name": "trucks",
        "description": "Truck route CRUD and search.",
    },
    {
        "name": "bookings",
        "description": "Book an available truck route.",
    },
    {
        "name": "notifications",
        "description": "In-app notifications for users.",
    },
    {
        "name": "admin",
        "description": "Admin KYC review and analytics dashboard.",
    },
    {
        "name": "history",
        "description": "Recent activity: last 10 searches and last 10 posts for the logged-in user.",
    },
    {
        "name": "misc",
        "description": "Health check and reference data.",
    },
]


def get_servers() -> list[dict]:
    local_url = f"http://localhost:{PORT}"
    base_url = os.environ.get("API_BASE_URL", local_url).rstrip("/")
    return [
        {"url": base_url, "description": "Current environment"},
        {"url": local_url, "description": "Local development"},
    ]


API_METADATA = {
    "title": API_TITLE,
    "version": API_VERSION,
    "description": API_DESCRIPTION,
    "contact": {
        "name": "Convoy Support",
        "url": "https://github.com/SkandTrizip/convoy_prod",
    },
    "license_info": {
        "name": "Proprietary",
    },
}
