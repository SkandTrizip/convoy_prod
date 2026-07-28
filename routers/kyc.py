from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import KYCRecord, User
from middleware.auth import require_path_user
from db.serializers import kyc_to_dict, parse_uuid
from models import (
    AadhaarOcrRequest,
    AadhaarSendOtpRequest,
    AadhaarVerifyOtpRequest,
    KYCSubmission,
)
from services.kyc import (
    normalize_aadhaar,
    send_aadhaar_otp,
    verify_aadhaar_otp,
    verify_aadhaar_with_smart_ocr,
    verify_kyc_with_cashfree,
)
from services.notifications import send_push_notification

router = APIRouter(prefix="/kyc", tags=["kyc"])


async def _save_verified_aadhaar_step(
    session: AsyncSession,
    user: User,
    kyc_record: KYCRecord | None,
    *,
    method: str,
    verified_data: dict,
    extra_data: dict | None = None,
    front_image: str | None = None,
    back_image: str | None = None,
) -> KYCRecord:
    """Record a successful Aadhaar verification. This is step 1 of 2 — the
    user still has to upload their KYC photo before kyc_status becomes
    'approved' (see POST /user/profile-photo/{user_id})."""
    now = datetime.utcnow()
    record_data = {**(extra_data or {}), "verified": verified_data}

    if kyc_record:
        kyc_record.method = method
        kyc_record.data = record_data
        kyc_record.submitted_date = now
        kyc_record.reviewed_date = now
        kyc_record.status = "approved"
        kyc_record.reviewed_by = "cashfree"
        if front_image:
            kyc_record.aadhaar_front_image = front_image
        if back_image:
            kyc_record.aadhaar_back_image = back_image
    else:
        kyc_record = KYCRecord(
            user_id=user.id,
            method=method,
            data=record_data,
            submitted_date=now,
            reviewed_date=now,
            status="approved",
            reviewed_by="cashfree",
            aadhaar_front_image=front_image,
            aadhaar_back_image=back_image,
        )
        session.add(kyc_record)

    verified_name = verified_data.get("name")
    if verified_name:
        user.name = verified_name

    await session.execute(
        update(User).where(User.id == user.id).values(
            kyc_status="in_progress",
            kyc_step="photo",
            name=user.name,
        )
    )
    await session.commit()
    await session.refresh(kyc_record)
    return kyc_record


@router.post("/aadhaar/send-otp/{user_id}")
async def send_kyc_aadhaar_otp(
    user_id: str,
    request: AadhaarSendOtpRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
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
            update(User)
            .where(User.id == user_uuid)
            .values(kyc_status="pending", kyc_step="aadhaar")
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
    _: User = Depends(require_path_user),
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
                kyc_status="in_progress",
                kyc_step="photo",
                name=user.name,
            )
        )
        await session.commit()

        await send_push_notification(
            user_id,
            "Aadhaar Verified",
            "Your Aadhaar has been verified. Upload your photo to complete KYC.",
            session=session,
        )

        return {
            "success": True,
            "status": "in_progress",
            "kycStep": "photo",
            "verifiedName": verified_name,
            "kyc": kyc_to_dict(kyc_record),
            "user": {
                "_id": str(user.id),
                "kycStatus": "in_progress",
                "kycStep": "photo",
                "name": user.name,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in verify_kyc_aadhaar_otp: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/aadhaar/ocr/{user_id}")
async def verify_kyc_aadhaar_ocr(
    user_id: str,
    request: AadhaarOcrRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Verify Aadhaar by uploading front/back images via Cashfree Smart OCR."""
    try:
        user_uuid = parse_uuid(user_id)
        user_result = await session.execute(select(User).where(User.id == user_uuid))
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        expected_aadhaar = None
        if request.aadhaarNumber:
            try:
                expected_aadhaar = normalize_aadhaar(request.aadhaarNumber)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

        ocr_result = verify_aadhaar_with_smart_ocr(
            front_image=request.aadhaarFrontImage,
            back_image=request.aadhaarBackImage,
            user_id=str(user_uuid),
            expected_aadhaar=expected_aadhaar,
        )
        if not ocr_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=ocr_result.get("message") or "Aadhaar OCR verification failed",
            )

        kyc_result = await session.execute(
            select(KYCRecord).where(KYCRecord.user_id == user_uuid)
        )
        existing_kyc = kyc_result.scalar_one_or_none()
        verified = ocr_result["verified_data"]
        kyc_record = await _save_verified_aadhaar_step(
            session,
            user,
            existing_kyc,
            method="aadhaar_ocr",
            verified_data=verified,
            extra_data={"ocr": ocr_result.get("ocr")},
            front_image=request.aadhaarFrontImage,
            back_image=request.aadhaarBackImage,
        )

        await send_push_notification(
            user_id,
            "Aadhaar Verified",
            "Your Aadhaar has been verified. Upload your photo to complete KYC.",
            session=session,
        )

        return {
            "success": True,
            "status": "in_progress",
            "kycStep": "photo",
            "verifiedName": verified.get("name"),
            "kyc": kyc_to_dict(kyc_record),
            "user": {
                "_id": str(user.id),
                "kycStatus": "in_progress",
                "kycStep": "photo",
                "name": user.name,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in verify_kyc_aadhaar_ocr: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/submit/{user_id}")
async def submit_kyc(
    user_id: str,
    submission: KYCSubmission,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Submit KYC with document images — verified via Cashfree Smart OCR when images provided."""
    try:
        if submission.method == "digilocker":
            raise HTTPException(
                status_code=400,
                detail="Use /kyc/aadhaar/send-otp and /kyc/aadhaar/verify for Aadhaar KYC",
            )

        user_uuid = parse_uuid(user_id)
        user_result = await session.execute(select(User).where(User.id == user_uuid))
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

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
            user_id=str(user_uuid),
        )
        status = verification.get("status", "under_review")

        if status == "approved" and verification.get("verified_data"):
            kyc_record = await _save_verified_aadhaar_step(
                session,
                user,
                existing_kyc,
                method=submission.method or "aadhaar_ocr",
                verified_data=verification["verified_data"],
                extra_data={
                    **submission.data,
                    "verification": verification,
                },
                front_image=submission.aadhaarFrontImage,
                back_image=submission.aadhaarBackImage,
            )
            await send_push_notification(
                user_id,
                "Aadhaar Verified",
                "Your Aadhaar has been verified. Upload your photo to complete KYC.",
                session=session,
            )
            return {
                "success": True,
                "status": "in_progress",
                "kycStep": "photo",
                "verifiedName": verification["verified_data"].get("name"),
                "kyc": kyc_to_dict(kyc_record),
            }

        if status == "rejected":
            raise HTTPException(
                status_code=400,
                detail=verification.get("message") or "KYC document verification failed",
            )

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
    _: User = Depends(require_path_user),
):
    """Get KYC status for user"""
    try:
        user_uuid = parse_uuid(user_id)
        user_result = await session.execute(select(User).where(User.id == user_uuid))
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        result = await session.execute(
            select(KYCRecord).where(KYCRecord.user_id == user_uuid)
        )
        kyc_record = result.scalar_one_or_none()

        if not kyc_record:
            return {
                "success": True,
                "status": user.kyc_status,
                "kycStep": user.kyc_step,
                "data": None,
            }

        return {
            "success": True,
            "status": user.kyc_status,
            "kycStep": user.kyc_step,
            "kyc": kyc_to_dict(kyc_record),
        }
    except Exception as e:
        logger.error("Error in get_kyc_status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
