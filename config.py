import os
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

# Third-party API Configuration
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")
CASHFREE_CLIENT_ID = os.environ.get("CASHFREE_CLIENT_ID")
CASHFREE_CLIENT_SECRET = os.environ.get("CASHFREE_CLIENT_SECRET")
CASHFREE_ENVIRONMENT = os.environ.get("CASHFREE_ENVIRONMENT", "production")

# JWT (API auth after OTP login)
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-insecure-change-in-production")
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "168"))
# ULIP proxy (EC2) — VAHAN / SARATHI verification
ULIP_PROXY_URL = os.environ.get("ULIP_PROXY_URL")
ULIP_PROXY_API_KEY = os.environ.get("ULIP_PROXY_API_KEY")
