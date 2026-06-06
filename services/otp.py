import random
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import OTP_EXPIRY_MINUTES
from db.base import LoginOTP


def generate_otp() -> str:
    """Generate a 4-digit OTP."""
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
    """Validate OTP against the latest unexpired record for the mobile."""
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
