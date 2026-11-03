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

# After a truck post (or reactivation) the app reports the poster's location now and then, for
# this long, so service requests can find them. Not live tracking: one update every
# REPORT_INTERVAL_MINUTES from a background job; the server drops anything faster than
# REPORT_MIN_SECONDS (e.g. an app-open update right after a background one).
LOCATION_TRACKING_HOURS = int(os.environ.get("LOCATION_TRACKING_HOURS", "48"))
LOCATION_REPORT_INTERVAL_MINUTES = int(os.environ.get("LOCATION_REPORT_INTERVAL_MINUTES", "30"))
LOCATION_REPORT_MIN_SECONDS = int(os.environ.get("LOCATION_REPORT_MIN_SECONDS", "60"))

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
MIN_REDEEM_AMOUNT_INR = Decimal(os.environ.get("MIN_REDEEM_AMOUNT_INR", "250"))

# Paid 1:1 calls (request -> provider accepts -> wallet hold -> call -> settle).
# See services/call_requests.py for how each of these is used.
CALL_MIN_AMOUNT_INR = Decimal(os.environ.get("CALL_MIN_AMOUNT_INR", "50"))
CALL_MAX_AMOUNT_INR = Decimal(os.environ.get("CALL_MAX_AMOUNT_INR", "10000"))
# The "cost of connecting": max(25% of the offer, Rs25), never more than the offer
# itself. Frozen at request time; owed to the provider if the call connects for
# CALL_CONNECT_THRESHOLD_SECONDS even when the requester is unhappy.
CALL_HOLD_FRACTION = Decimal("0.25")
CALL_HOLD_MIN_INR = Decimal("25")
CALL_CONNECT_THRESHOLD_SECONDS = int(os.environ.get("CALL_CONNECT_THRESHOLD_SECONDS", "30"))
CALL_DURATION_OPTIONS_MINUTES = (10, 15, 30)
# One window from creation covers pending + accepted + ringing; then the hold is released.
CALL_REQUEST_TTL_MINUTES = int(os.environ.get("CALL_REQUEST_TTL_MINUTES", "30"))
CALL_RING_SECONDS = int(os.environ.get("CALL_RING_SECONDS", "45"))
# After a call that connected, the requester gets this long to answer "were you
# satisfied?"; no answer means they pay only the call amount.
CALL_FEEDBACK_TTL_HOURS = int(os.environ.get("CALL_FEEDBACK_TTL_HOURS", "24"))
CALL_SWEEP_INTERVAL_SECONDS = int(os.environ.get("CALL_SWEEP_INTERVAL_SECONDS", "30"))
# A live call: each app reports in every few seconds. A side counts as "in the call"
# if the server heard from it within HEARTBEAT_GRACE; time counts toward the 30 s
# threshold and the call amount only while BOTH sides are in. A drop gets
# RECONNECT_WINDOW to come back before the call is ended. The call is force-ended
# END_GRACE after the chosen duration (its tokens expire at about the same time).
CALL_HEARTBEAT_GRACE_SECONDS = int(os.environ.get("CALL_HEARTBEAT_GRACE_SECONDS", "15"))
CALL_RECONNECT_WINDOW_SECONDS = int(os.environ.get("CALL_RECONNECT_WINDOW_SECONDS", "60"))
CALL_END_GRACE_SECONDS = int(os.environ.get("CALL_END_GRACE_SECONDS", "30"))
# A person signed in on two phones: the phone on a live call keeps it. Another of their phones
# may take it over (or hang it up) only after the first has been silent this long (a dead battery).
CALL_DEVICE_TAKEOVER_SECONDS = int(os.environ.get("CALL_DEVICE_TAKEOVER_SECONDS", "20"))

# DEV ONLY: lets a user credit their OWN wallet with test money until the payment
# gateway lands. Off unless explicitly "true". Local dev shares the production
# database, so never switch this on while pointed at it (see my_task.md).
DEV_WALLET_CREDIT_ENABLED = os.environ.get("DEV_WALLET_CREDIT_ENABLED", "false").lower() == "true"
DEV_WALLET_CREDIT_MAX_INR = Decimal(os.environ.get("DEV_WALLET_CREDIT_MAX_INR", "10000"))

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


# --- Apple VoIP push (the iPhone's incoming-call screen) -----------------------------
# Leave these unset and the iPhone simply gets the normal notification instead.
# Create the key at developer.apple.com > Keys (Apple Push Notifications service), then either
# put the .p8 file's path in APNS_KEY_PATH or its whole text in APNS_KEY_P8 (use \n for line breaks).
APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "")
APNS_KEY_PATH = os.environ.get("APNS_KEY_PATH", "")
APNS_KEY_P8 = os.environ.get("APNS_KEY_P8", "")
APNS_BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "com.sharkship.bonut")

# --- Wallet recharge through Razorpay ---------------------------------------------------
# Leave RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET unset and recharging is simply off.
# RAZORPAY_WEBHOOK_SECRET is the secret you type when you create the webhook in the Razorpay
# dashboard (event "payment.captured", URL <api>/api/webhooks/razorpay).
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
RECHARGE_MIN_INR = Decimal(os.environ.get("RECHARGE_MIN_INR", "10"))
RECHARGE_MAX_INR = Decimal(os.environ.get("RECHARGE_MAX_INR", "10000"))
# The gateway's fee, which the person recharging pays ON TOP of what they want in the wallet. Razorpay's
# standard 2% plus 18% GST on that 2% is 2.36%; set it to what your Razorpay contract says.
RECHARGE_FEE_PERCENT = Decimal(os.environ.get("RECHARGE_FEE_PERCENT", "2.36"))
RECHARGE_FEE_FIXED_INR = Decimal(os.environ.get("RECHARGE_FEE_FIXED_INR", "0"))
