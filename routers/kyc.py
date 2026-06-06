from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import KYCRecord, User
from db.serializers import kyc_to_dict, parse_uuid
from models import (
    AadhaarSendOtpRequest,
    AadhaarVerifyOtpRequest,
    KYCSubmission,
)
from services.kyc import (
    normalize_aadhaar,
    send_aadhaar_otp,
    verify_aadhaar_otp,
    verify_kyc_with_cashfree,
)
from services.notifications import send_expo_push_notification

router = APIRouter(prefix="/kyc", tags=["kyc"])


@router.post("/aadhaar/send-otp/{user_id}")
async def send_kyc_aadhaar_otp(
    user_id: str,
    request: AadhaarSendOtpRequest,
    session: AsyncSession = Depends(get_session),
):
    """Send OTP to the mobile number linked with the Aadhaar."""
    try:
        user_uuid = parse_uuid(user_id)
        user_result = await session.execute(select(User).where(User.id == user_uuid))
        if not user_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="User not found")

        try:
            aadhaar = normalize_aadhaar(request.aadhaarNumber)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        result = send_aadhaar_otp(aadhaar)
        if not result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=result.get("message") or "Failed to send Aadhaar OTP",
            )

        now = datetime.utcnow()
        pending_data = {
            "aadhaar_last4": aadhaar[-4:],
            "ref_id": result["ref_id"],
            "otp_sent_at": now.isoformat(),
        }

        kyc_result = await session.execute(
            select(KYCRecord).where(KYCRecord.user_id == user_uuid)
        )
        existing_kyc = kyc_result.scalar_one_or_none()

        if existing_kyc:
            existing_kyc.method = "aadhaar_otp"
            existing_kyc.data = pending_data
            existing_kyc.submitted_date = now
            existing_kyc.reviewed_date = None
            existing_kyc.status = "pending_otp"
            existing_kyc.reviewed_by = None
        else:
            session.add(
                KYCRecord(
                    user_id=user_uuid,
                    method="aadhaar_otp",
                    data=pending_data,
                    submitted_date=now,
                    status="pending_otp",
                )
            )

        await session.execute(
            update(User).where(User.id == user_uuid).values(kyc_status="pending")
        )
        await session.commit()

        response = {
            "success": True,
            "message": result.get("message"),
            "refId": result["ref_id"],
        }
        if result.get("dev_mode"):
            response["devMode"] = True
            response["hint"] = "Dev mode: use OTP 111000 to verify"

        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in send_kyc_aadhaar_otp: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/aadhaar/verify/{user_id}")
async def verify_kyc_aadhaar_otp(
    user_id: str,
    request: AadhaarVerifyOtpRequest,
    session: AsyncSession = Depends(get_session),
):
    """Verify Aadhaar OTP via Cashfree and approve KYC on success."""
    try:
        user_uuid = parse_uuid(user_id)
        user_result = await session.execute(select(User).where(User.id == user_uuid))
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        kyc_result = await session.execute(
            select(KYCRecord).where(KYCRecord.user_id == user_uuid)
        )
        kyc_record = kyc_result.scalar_one_or_none()
        if not kyc_record or kyc_record.status != "pending_otp":
            raise HTTPException(
                status_code=400,
                detail="No pending Aadhaar verification. Send OTP first.",
            )

        stored_ref = str(kyc_record.data.get("ref_id", ""))
        if stored_ref != str(request.refId):
            raise HTTPException(status_code=400, detail="Invalid verification session")

        try:
            aadhaar = normalize_aadhaar(request.aadhaarNumber)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        if kyc_record.data.get("aadhaar_last4") != aadhaar[-4:]:
            raise HTTPException(
                status_code=400,
                detail="Aadhaar number does not match the OTP request",
            )

        verify_result = verify_aadhaar_otp(request.refId, request.otp)
        if not verify_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=verify_result.get("message") or "Aadhaar verification failed",
            )

        verified = verify_result["verified_data"]
        now = datetime.utcnow()
        kyc_record.method = "aadhaar_otp"
        kyc_record.data = {
            "aadhaar_last4": aadhaar[-4:],
            "ref_id": request.refId,
            "verified": verified,
        }
        kyc_record.submitted_date = now
        kyc_record.reviewed_date = now
        kyc_record.status = "approved"

        verified_name = verified.get("name")
        if verified_name:
            user.name = verified_name

        await session.execute(
            update(User).where(User.id == user_uuid).values(
                kyc_status="approved",
                name=user.name,
            )
        )
        await session.commit()

        await send_expo_push_notification(
            user_id,
            "KYC Approved",
            "Your Aadhaar has been verified. You can now add vehicles.",
            session=session,
        )

        return {
            "success": True,
            "status": "approved",
            "verifiedName": verified_name,
            "kyc": kyc_to_dict(kyc_record),
            "user": {
                "_id": str(user.id),
                "kycStatus": "approved",
                "name": user.name,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in verify_kyc_aadhaar_otp: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/submit/{user_id}")
async def submit_kyc(
    user_id: str,
    submission: KYCSubmission,
    session: AsyncSession = Depends(get_session),
):
    """Submit manual KYC with document images for admin review."""
    try:
        if submission.method == "digilocker":
            raise HTTPException(
                status_code=400,
                detail="Use /kyc/aadhaar/send-otp and /kyc/aadhaar/verify for Aadhaar KYC",
            )

        user_uuid = parse_uuid(user_id)
        result = await session.execute(
            select(KYCRecord).where(KYCRecord.user_id == user_uuid)
        )
        existing_kyc = result.scalar_one_or_none()

        verification = verify_kyc_with_cashfree(
            submission.data,
            {
                "front": submission.aadhaarFrontImage,
                "back": submission.aadhaarBackImage,
            },
        )
        status = verification.get("status", "under_review")
        now = datetime.utcnow()

        if existing_kyc:
            existing_kyc.method = submission.method
            existing_kyc.data = {**submission.data, "verification": verification}
            existing_kyc.submitted_date = now
            existing_kyc.reviewed_date = None
            existing_kyc.reviewed_by = None
            existing_kyc.status = status
            existing_kyc.aadhaar_front_image = submission.aadhaarFrontImage
            existing_kyc.aadhaar_back_image = submission.aadhaarBackImage
        else:
            session.add(
                KYCRecord(
                    user_id=user_uuid,
                    method=submission.method,
                    data={**submission.data, "verification": verification},
                    submitted_date=now,
                    status=status,
                    aadhaar_front_image=submission.aadhaarFrontImage,
                    aadhaar_back_image=submission.aadhaarBackImage,
                )
            )

        await session.execute(
            update(User).where(User.id == user_uuid).values(kyc_status=status)
        )
        await session.commit()

        return {"success": True, "status": status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in submit_kyc: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{user_id}")
async def get_kyc_status(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get KYC status for user"""
    try:
        result = await session.execute(
            select(KYCRecord).where(KYCRecord.user_id == parse_uuid(user_id))
        )
        kyc_record = result.scalar_one_or_none()

        if not kyc_record:
            return {"success": True, "status": "pending", "data": None}

        return {"success": True, "kyc": kyc_to_dict(kyc_record)}
    except Exception as e:
        logger.error("Error in get_kyc_status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
