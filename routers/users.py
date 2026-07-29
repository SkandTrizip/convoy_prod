from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import PROFILE_PHOTO_MAX_SIZE_MB, logger
from database import get_session
from db.base import User
from db.serializers import parse_uuid, user_to_dict
from middleware.auth import get_current_user, require_path_user
from models import DeviceLogoutRequest, DeviceRegisterRequest, PushTokenRequest, UserProfile
from notifications.repositories import device_repository
from services.blob_storage import (
    ALLOWED_CONTENT_TYPES,
    delete_profile_photo,
    download_profile_photo,
    upload_profile_photo,
)
from services.notifications import send_push_notification

router = APIRouter(prefix="/user", tags=["user"])


@router.get("/profile/{user_id}")
async def get_user_profile(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Get user profile"""
    try:
        result = await session.execute(
            select(User).where(User.id == parse_uuid(user_id))
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        return {"success": True, "user": user_to_dict(user)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_user_profile: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/profile/{user_id}")
async def update_user_profile(
    user_id: str,
    profile: UserProfile,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Update user profile"""
    try:
        update_data = {}
        if profile.name:
            update_data["name"] = profile.name
        if profile.profilePhoto:
            update_data["profile_photo"] = profile.profilePhoto

        if not update_data:
            result = await session.execute(
                select(User).where(User.id == parse_uuid(user_id))
            )
            if not result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="User not found")
            return {"success": True, "message": "Profile updated successfully"}

        result = await session.execute(
            update(User)
            .where(User.id == parse_uuid(user_id))
            .values(**update_data)
        )

        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")

        await session.commit()
        return {"success": True, "message": "Profile updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in update_user_profile: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/profile-photo/{user_id}")
async def upload_user_profile_photo(
    user_id: str,
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Upload/replace the user's profile photo (stored in a private Azure blob container)"""
    try:
        if file.content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported image type '{file.content_type}'. Allowed: {', '.join(ALLOWED_CONTENT_TYPES)}",
            )

        content = await file.read()
        max_bytes = PROFILE_PHOTO_MAX_SIZE_MB * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"Image exceeds {PROFILE_PHOTO_MAX_SIZE_MB}MB limit",
            )

        result = await session.execute(select(User).where(User.id == parse_uuid(user_id)))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        old_photo = user.profile_photo
        blob_name = upload_profile_photo(user_id, content, file.content_type)

        # Uploading the photo is step 2 of KYC — completes it if step 1
        # (Aadhaar verification) is already done.
        completes_kyc = user.kyc_step == "photo"
        update_values = {"profile_photo": blob_name}
        if completes_kyc:
            update_values["kyc_status"] = "approved"
            update_values["kyc_step"] = "completed"

        await session.execute(
            update(User).where(User.id == parse_uuid(user_id)).values(**update_values)
        )
        await session.commit()

        if old_photo and not old_photo.startswith("http"):
            delete_profile_photo(old_photo)

        if completes_kyc:
            await send_push_notification(
                user_id,
                "KYC Approved",
                "Your KYC is now complete. You can now add vehicles.",
                session=session,
            )

        return {
            "success": True,
            "profilePhoto": f"/api/user/profile-photo/{user_id}",
            "kycStatus": "approved" if completes_kyc else user.kyc_status,
            "kycStep": "completed" if completes_kyc else user.kyc_step,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in upload_user_profile_photo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/profile-photo/{user_id}")
async def get_user_profile_photo(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(get_current_user),
):
    """Stream a user's profile photo from blob storage (keeps the container private)"""
    try:
        result = await session.execute(select(User).where(User.id == parse_uuid(user_id)))
        user = result.scalar_one_or_none()
        if not user or not user.profile_photo or user.profile_photo.startswith("http"):
            raise HTTPException(status_code=404, detail="Profile photo not found")

        downloaded = download_profile_photo(user.profile_photo)
        if not downloaded:
            raise HTTPException(status_code=404, detail="Profile photo not found")

        content, content_type = downloaded
        return Response(content=content, media_type=content_type)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_user_profile_photo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/push-token/{user_id}")
async def update_push_token(
    user_id: str,
    data: PushTokenRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Legacy single-token registration, kept for un-upgraded app versions.
    Routes into the same per-device table as /devices/register, under one
    synthesized device slot per user — matches this endpoint's old
    single-token-per-user behavior exactly, so an old app keeps working
    unchanged. Superseded by /devices/register; remove once app adoption of
    the new endpoint is confirmed."""
    try:
        await device_repository.upsert_device(
            session,
            user_id=parse_uuid(user_id),
            device_id=f"legacy-{user_id}",
            platform="unknown",
            fcm_token=data.pushToken,
        )
        await session.commit()

        return {"success": True, "message": "Push token updated"}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error updating push token: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/devices/register/{user_id}")
async def register_device(
    user_id: str,
    data: DeviceRegisterRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Idempotent device sync — safe to call repeatedly (app launch with a
    changed token, FCM's onTokenRefresh, etc). Upserts by device_id."""
    try:
        await device_repository.upsert_device(
            session,
            user_id=parse_uuid(user_id),
            device_id=data.deviceId,
            platform=data.platform,
            fcm_token=data.fcmToken,
            app_version=data.appVersion,
            device_name=data.deviceName,
        )
        await session.commit()

        return {"success": True, "message": "Device registered"}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error registering device: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/devices/logout/{user_id}")
async def logout_device(
    user_id: str,
    data: DeviceLogoutRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Deregister push for exactly one of the user's devices — does not
    touch the driver JWT (stateless, not revocable) or any other device."""
    try:
        deregistered = await device_repository.logout_device(
            session, user_id=parse_uuid(user_id), device_id=data.deviceId
        )
        if not deregistered:
            raise HTTPException(status_code=404, detail="Device not found for this user")
        await session.commit()

        return {"success": True, "message": "Device logged out"}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error logging out device: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
