import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from logging_config import setup_logging

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
PORT = int(os.environ.get("PORT", "8000"))
logger = setup_logging(LOG_LEVEL)

# Veup SMS (login OTP)
VEUP_PROCESS_KEY = os.environ.get("VEUP_PROCESS_KEY")
VEUP_SENDER_ID = os.environ.get("VEUP_SENDER_ID", "SHRKSP")
VEUP_TEMPLATE_ID = os.environ.get("VEUP_TEMPLATE_ID", "1707176733907742920")
VEUP_CAMPAIGN_NAME = os.environ.get("VEUP_CAMPAIGN_NAME", "shipment_tracking")
OTP_EXPIRY_MINUTES = int(os.environ.get("OTP_EXPIRY_MINUTES", "10"))

# Truck post visibility (auto-expire + reactivate window)
POST_EXPIRE_HOURS = int(os.environ.get("POST_EXPIRE_HOURS", "24"))
POST_EXPIRE_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("POST_EXPIRE_CHECK_INTERVAL_SECONDS", "300")
)

# Stale push-device cleanup (judged on last_app_seen, not last_token_sync —
# see db/base.py's UserDevice)
DEVICE_CLEANUP_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("DEVICE_CLEANUP_CHECK_INTERVAL_SECONDS", str(24 * 60 * 60))
)
DEVICE_DEACTIVATE_AFTER_DAYS = int(os.environ.get("DEVICE_DEACTIVATE_AFTER_DAYS", "180"))
DEVICE_DELETE_AFTER_INACTIVE_DAYS = int(os.environ.get("DEVICE_DELETE_AFTER_INACTIVE_DAYS", "90"))

# Third-party API Configuration
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")
CASHFREE_CLIENT_ID = os.environ.get("CASHFREE_CLIENT_ID")
CASHFREE_CLIENT_SECRET = os.environ.get("CASHFREE_CLIENT_SECRET")
CASHFREE_ENVIRONMENT = os.environ.get("CASHFREE_ENVIRONMENT", "production")

# JWT (API auth after OTP login)
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-insecure-change-in-production")
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "168"))

# Admin JWT (email+password login, /api/admin-auth/*) — separate secret/namespace
# from driver JWT above, so an admin token and a driver token are never
# interchangeable even if both happened to reuse the same subject id.
ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "dev-insecure-admin-change-in-production")
ADMIN_JWT_EXPIRE_HOURS = int(os.environ.get("ADMIN_JWT_EXPIRE_HOURS", "24"))

# ULIP (direct) — VAHAN / SARATHI verification from this server
ULIP_BASE_URL = os.environ.get(
    "ULIP_BASE_URL", "https://www.ulip.dpiit.gov.in/ulip/v1.0.0"
)
ULIP_LOGIN_URL = os.environ.get("ULIP_LOGIN_URL")
ULIP_USERNAME = os.environ.get("ULIP_USERNAME")
ULIP_PASSWORD = os.environ.get("ULIP_PASSWORD")
ULIP_VAHAN_API = os.environ.get("ULIP_VAHAN_API", "VAHAN/04")

# Azure Blob Storage (profile photos) — private container, served via backend proxy
AZURE_CONN_STR = os.environ.get("AZURE_CONN_STR")
AZURE_CONTAINER_NAME = os.environ.get("AZURE_CONTAINER_NAME", "trizip")
PROFILE_PHOTO_MAX_SIZE_MB = int(os.environ.get("PROFILE_PHOTO_MAX_SIZE_MB", "5"))

# Wallet (reward/scratch-card payouts)
MIN_REDEEM_AMOUNT_INR = Decimal(os.environ.get("MIN_REDEEM_AMOUNT_INR", "500"))

# Firebase Cloud Messaging (push notifications) — service account JSON path
FIREBASE_CREDENTIALS_PATH = os.environ.get(
    "FIREBASE_CREDENTIALS_PATH", str(ROOT_DIR / "credentials" / "firebase-admin.json")
)
