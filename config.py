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

# HMAC key for hashing Aadhaar numbers (one-way, for cross-account duplicate
# detection) — deliberately separate from JWT_SECRET, own rotation policy.
AADHAAR_HASH_SECRET = os.environ.get("AADHAAR_HASH_SECRET", "dev-insecure-aadhaar-change-in-production")

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

# Scratch-card expiry sweep (unrevealed cards past expires_at -> "expired")
SCRATCH_CARD_EXPIRY_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("SCRATCH_CARD_EXPIRY_CHECK_INTERVAL_SECONDS", str(60 * 60))
)

# Firebase Cloud Messaging (push notifications) — service account JSON path
FIREBASE_CREDENTIALS_PATH = os.environ.get(
    "FIREBASE_CREDENTIALS_PATH", str(ROOT_DIR / "credentials" / "firebase-admin.json")
)

# Bunny Stream (video reels) — not configured -> upload/sync endpoints fail with a
# clear 503 rather than crashing at import time, matching this repo's other
# "not configured -> graceful failure/dev mode" integrations.
BUNNY_STREAM_API_KEY = os.environ.get("BUNNY_STREAM_API_KEY")
BUNNY_STREAM_LIBRARY_ID = os.environ.get("BUNNY_STREAM_LIBRARY_ID")
BUNNY_CDN_HOSTNAME = os.environ.get("BUNNY_CDN_HOSTNAME")
MAX_VIDEO_DURATION_SECONDS = int(os.environ.get("MAX_VIDEO_DURATION_SECONDS", "30"))

# Agora (1:1 video calls) — same not-configured -> 503 behavior as Bunny above.
AGORA_APP_ID = os.environ.get("AGORA_APP_ID")
AGORA_APP_CERTIFICATE = os.environ.get("AGORA_APP_CERTIFICATE")
AGORA_TOKEN_EXPIRATION_SECONDS = int(os.environ.get("AGORA_TOKEN_EXPIRATION_SECONDS", "3600"))
# Hard cap on a call's lifetime: no token minted for a call (host or guest, at
# start or on join) outlives `started_at + this`, regardless of when it's
# requested — see services/agora_client.py.
AGORA_MAX_CALL_SECONDS = int(os.environ.get("AGORA_MAX_CALL_SECONDS", "3600"))

# Video feed prefetch caching (Redis cache/locks + RabbitMQ job queue consumed by
# worker.py). Best-effort only — a missing/unreachable Redis or RabbitMQ degrades
# to "always fetch from Bunny directly", never breaks playback.
REDIS_URL = os.environ.get("REDIS_URL")
RABBITMQ_URL = os.environ.get("RABBITMQ_URL")
PREFETCH_NEIGHBOR_WINDOW = int(os.environ.get("PREFETCH_NEIGHBOR_WINDOW", "3"))
PREFETCH_SEGMENT_COUNT = int(os.environ.get("PREFETCH_SEGMENT_COUNT", "1"))
PREFETCH_CACHE_TTL_SECONDS = int(os.environ.get("PREFETCH_CACHE_TTL_SECONDS", "120"))
PREFETCH_LOCK_TTL_SECONDS = int(os.environ.get("PREFETCH_LOCK_TTL_SECONDS", "30"))

# Reel view counting (services/view_counter.py): views are buffered in Redis and
# flushed to videos.view_count in batches. Repeat reports of the same user+reel
# inside the dedupe window are ignored (client retry / replay-loop protection).
REEL_VIEW_FLUSH_INTERVAL_SECONDS = int(os.environ.get("REEL_VIEW_FLUSH_INTERVAL_SECONDS", "10"))
REEL_VIEW_DEDUPE_SECONDS = int(os.environ.get("REEL_VIEW_DEDUPE_SECONDS", "30"))

# Heatmap (services/heatmap.py): the reels heatmap only counts reels posted
# within this many days, so it reflects current activity rather than every
# reel ever posted. The trucks heatmap needs no window — it only ever counts
# live (unexpired) posts.
HEATMAP_REEL_WINDOW_DAYS = int(os.environ.get("HEATMAP_REEL_WINDOW_DAYS", "30"))
