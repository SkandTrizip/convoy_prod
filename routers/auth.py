from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import User
from db.serializers import user_to_dict
from middleware.auth import create_access_token
from models import SendOTPRequest, VerifyOTPRequest
from services.otp import generate_otp, store_otp, verify_stored_otp
from services.sms import is_sms_dev_mode, send_otp_sms

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/send-otp")
async def send_otp(
    request: SendOTPRequest,
    session: AsyncSession = Depends(get_session),
):
    """Send OTP to mobile number"""
    try:
        mobile = request.mobile
        otp = generate_otp(mobile)

        await store_otp(session, mobile, otp)
        sms_sent = send_otp_sms(mobile, otp)

        logger.info(f"OTP generated for {mobile}")

        # Dev mode or Veup failure: return OTP in JSON so login still works.
        include_otp = is_sms_dev_mode() or not sms_sent
        if not sms_sent and not is_sms_dev_mode():
            logger.warning("SMS delivery failed for %s; returning OTP in API response", mobile)

        return {
            "success": True,
            "message": "OTP sent successfully" if sms_sent else "OTP generated (SMS not delivered)",
            "otp": otp if include_otp else None,
        }
    except Exception as e:
        logger.error(f"Error in send_otp: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/verify-otp")
async def verify_otp(
    request: VerifyOTPRequest,
    session: AsyncSession = Depends(get_session),
):
    """Verify OTP and login/register user"""
    try:
        mobile = request.mobile
        otp = request.otp

        if not await verify_stored_otp(session, mobile, otp):
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")

        result = await session.execute(select(User).where(User.mobile == mobile))
        user = result.scalar_one_or_none()

        if not user:
            user = User(mobile=mobile)
            session.add(user)
            await session.commit()
            await session.refresh(user)

        access_token = create_access_token(str(user.id), user.mobile)

        return {
            "success": True,
            "message": "Login successful",
            "accessToken": access_token,
            "tokenType": "bearer",
            "user": user_to_dict(user),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in verify_otp: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
