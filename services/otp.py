import random
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import OTP_EXPIRY_MINUTES
from db.base import LoginOTP

# --- Hardcoded QA/testing OTPs -----------------------------------------
# These numbers always use TEST_OTP, in every environment. Remove before
# these numbers are ever used by real users.
TEST_OTP = "1234"
TEST_MOBILE_NUMBERS = {"9876543210", "7408681257"}


def _normalize_mobile(mobile: str) -> str:
    """Strip non-digits and a leading '91' country code, e.g. '+919876543210' -> '9876543210'."""
    digits = "".join(c for c in mobile if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    return digits


def _is_test_mobile(mobile: str) -> bool:
    return _normalize_mobile(mobile) in TEST_MOBILE_NUMBERS


def generate_otp(mobile: str | None = None) -> str:
    """Generate a 4-digit OTP. Whitelisted test numbers always get TEST_OTP."""
    if mobile and _is_test_mobile(mobile):
        return TEST_OTP
    return f"{random.randint(0, 9999):04d}"


async def store_otp(session: AsyncSession, mobile: str, otp: str) -> LoginOTP:
    """Persist OTP until expiry; invalidate any previous unverified OTPs for this mobile."""
    now = datetime.utcnow()
    expires_at = now + timedelta(minutes=OTP_EXPIRY_MINUTES)

    await session.execute(
        delete(LoginOTP).where(
            LoginOTP.mobile == mobile,
            LoginOTP.verified.is_(False),
        )
    )

    record = LoginOTP(
        mobile=mobile,
        otp=otp,
        created_at=now,
        expires_at=expires_at,
        verified=False,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def verify_stored_otp(session: AsyncSession, mobile: str, otp: str) -> bool:
    """Validate OTP against the latest unexpired record for the mobile.

    Whitelisted test numbers always accept TEST_OTP, bypassing the DB
    lookup entirely (no prior send-otp call required).
    """
    if _is_test_mobile(mobile) and otp == TEST_OTP:
        return True

    now = datetime.utcnow()
    result = await session.execute(
        select(LoginOTP)
        .where(
            LoginOTP.mobile == mobile,
            LoginOTP.verified.is_(False),
            LoginOTP.expires_at > now,
        )
        .order_by(LoginOTP.created_at.desc())
        .limit(1)
    )
    record = result.scalar_one_or_none()
    if not record:
        return False
    if record.otp != otp:
        return False

    record.verified = True
    await session.commit()
    return True
