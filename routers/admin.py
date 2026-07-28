from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import PROFILE_PHOTO_MAX_SIZE_MB, logger
from database import get_session
from db.base import AdminUser, CallLog, SearchDemand, Truck, TruckRoute, User
from db.serializers import parse_uuid, user_to_dict
from middleware.admin_auth import get_current_admin
from models import AdminRedeemAction, AdminUpdateUserRequest, SendNotificationBatchRequest
from notifications.repositories import device_repository, notification_repository
from notifications.sender.firebase_sender import PreparedMessage, send_batch
from services.blob_storage import (
    ALLOWED_CONTENT_TYPES,
    delete_profile_photo,
    download_profile_photo,
    upload_profile_photo,
)
from services.notifications import send_push_notification
from services.user_deletion import delete_user_cascade
from services.wallet import list_pending_redeems, mark_redeem_paid, reject_redeem

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(get_current_admin)])


def _admin_user_to_dict(user: User) -> dict:
    """Same shape as user_to_dict, but profilePhoto points at the admin-auth'd
    proxy below instead of the driver-JWT-only one — an admin token can't
    call /api/user/profile-photo/{id}."""
    data = user_to_dict(user)
    if user.profile_photo and not user.profile_photo.startswith("http"):
        data["profilePhoto"] = f"/api/admin/users/{user.id}/profile-photo"
    return data


@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Matches mobile, name, or exact user ID"),
    session: AsyncSession = Depends(get_session),
):
    """Paginated user list for the admin panel, with optional search."""
    try:
        condition = None
        if search and search.strip():
            term = search.strip()
            pattern = f"%{term}%"
            clauses = [User.mobile.ilike(pattern), User.name.ilike(pattern)]
            try:
                clauses.append(User.id == parse_uuid(term))
            except ValueError:
                pass
            condition = or_(*clauses)

        count_stmt = select(func.count()).select_from(User)
        list_stmt = select(User).order_by(User.created_date.desc())
        if condition is not None:
            count_stmt = count_stmt.where(condition)
            list_stmt = list_stmt.where(condition)

        total = await session.scalar(count_stmt)
        list_stmt = list_stmt.offset((page - 1) * pageSize).limit(pageSize)
        result = await session.execute(list_stmt)
        users = result.scalars().all()

        return {
            "success": True,
            "users": [_admin_user_to_dict(u) for u in users],
            "page": page,
            "pageSize": pageSize,
            "total": total,
        }
    except Exception as e:
        logger.error(f"Error in list_users: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users/{user_id}/profile-photo")
async def admin_get_user_profile_photo(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Admin-auth'd equivalent of /api/user/profile-photo/{id} — streams a
    user's photo from blob storage using the admin JWT instead of a driver one."""
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
        logger.error(f"Error in admin_get_user_profile_photo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/users/{user_id}")
async def admin_update_user(
    user_id: str,
    payload: AdminUpdateUserRequest,
    session: AsyncSession = Depends(get_session),
):
    """Admin data-fix: rename a user. Deliberately narrow — mobile number
    stays locked to its OTP-verified identity, and this never touches
    kyc_status/kyc_step."""
    try:
        user_uuid = parse_uuid(user_id)
        result = await session.execute(
            update(User).where(User.id == user_uuid).values(name=payload.name)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")
        await session.commit()

        user_result = await session.execute(select(User).where(User.id == user_uuid))
        user = user_result.scalar_one()
        return {"success": True, "user": _admin_user_to_dict(user)}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in admin_update_user: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/users/{user_id}/profile-photo")
async def admin_upload_user_profile_photo(
    user_id: str,
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
):
    """Admin data-fix: replace a user's profile photo. Pure data edit —
    unlike the driver-facing upload endpoint, this never marks KYC complete."""
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

        user_uuid = parse_uuid(user_id)
        result = await session.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        old_photo = user.profile_photo
        blob_name = upload_profile_photo(user_id, content, file.content_type)

        await session.execute(
            update(User).where(User.id == user_uuid).values(profile_photo=blob_name)
        )
        await session.commit()

        if old_photo and not old_photo.startswith("http"):
            delete_profile_photo(old_photo)

        user_result = await session.execute(select(User).where(User.id == user_uuid))
        refreshed = user_result.scalar_one()
        return {"success": True, "user": _admin_user_to_dict(refreshed)}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in admin_upload_user_profile_photo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


async def _set_account_status(session: AsyncSession, user_id: str, status: str) -> dict:
    user_uuid = parse_uuid(user_id)
    result = await session.execute(
        update(User).where(User.id == user_uuid).values(account_status=status)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    await session.commit()

    user_result = await session.execute(select(User).where(User.id == user_uuid))
    user = user_result.scalar_one()
    return _admin_user_to_dict(user)


@router.post("/users/{user_id}/suspend")
async def admin_suspend_user(user_id: str, session: AsyncSession = Depends(get_session)):
    """Blocks the user's driver JWT (see middleware/auth.py) without deleting anything."""
    try:
        user = await _set_account_status(session, user_id, "suspended")
        return {"success": True, "message": "User suspended", "user": user}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in admin_suspend_user: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/users/{user_id}/reactivate")
async def admin_reactivate_user(user_id: str, session: AsyncSession = Depends(get_session)):
    try:
        user = await _set_account_status(session, user_id, "active")
        return {"success": True, "message": "User reactivated", "user": user}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in admin_reactivate_user: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analytics")
async def get_analytics(session: AsyncSession = Depends(get_session)):
    """Get analytics dashboard data"""
    try:
        now = datetime.utcnow()

        total_users = await session.scalar(select(func.count()).select_from(User))
        approved_kyc = await session.scalar(
            select(func.count()).select_from(User).where(User.kyc_status == "approved")
        )
        total_vehicles = await session.scalar(select(func.count()).select_from(Truck))
        verified_vehicles = await session.scalar(
            select(func.count())
            .select_from(Truck)
            .where(Truck.verification_status == "verified")
        )
        active_posts = await session.scalar(
            select(func.count())
            .select_from(TruckRoute)
            .where(
                TruckRoute.status.in_(("available", "active")),
                TruckRoute.expires_at > now,
            )
        )
        total_searches = await session.scalar(
            select(func.count()).select_from(SearchDemand)
        )
        total_calls = await session.scalar(select(func.count()).select_from(CallLog))

        origin_name = SearchDemand.origin["name"].as_string()
        destination_name = SearchDemand.destination["name"].as_string()

        top_routes_result = await session.execute(
            select(
                origin_name.label("origin"),
                destination_name.label("destination"),
                SearchDemand.truck_type,
                func.count().label("count"),
            )
            .group_by(origin_name, destination_name, SearchDemand.truck_type)
            .order_by(func.count().desc())
            .limit(10)
        )

        top_routes = [
            {
                "_id": {
                    "origin": row.origin,
                    "destination": row.destination,
                    "truckType": row.truck_type,
                },
                "count": row.count,
            }
            for row in top_routes_result
        ]

        return {
            "success": True,
            "analytics": {
                "totalUsers": total_users,
                "approvedKYC": approved_kyc,
                "totalVehicles": total_vehicles,
                "verifiedVehicles": verified_vehicles,
                "activePosts": active_posts,
                "totalSearches": total_searches,
                "totalCalls": total_calls,
                "topRoutes": top_routes,
            },
        }
    except Exception as e:
        logger.error(f"Error in get_analytics: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/users/delete/{user_id}")
async def delete_user_account(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Permanently delete a user and everything tied to them: truck posts (and their
    destinations), registered vehicles, KYC record, bookings (their own and other
    users' bookings on their posts), search-demand tracking, notifications, call logs,
    synced contacts, and pending login OTPs. Irreversible.
    """
    try:
        try:
            user_uuid = parse_uuid(user_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        result = await session.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        deleted = await delete_user_cascade(session, user)
        await session.commit()

        logger.warning("Admin deleted user %s (mobile=%s): %s", user_id, user.mobile, deleted)

        return {
            "success": True,
            "message": "User and all associated data permanently deleted",
            "deleted": deleted,
        }
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in delete_user_account: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


def _redeem_request_to_dict(redeem_req) -> dict:
    return {
        "id": str(redeem_req.id),
        "userId": str(redeem_req.user_id),
        "amount": str(redeem_req.amount),
        "upiId": redeem_req.upi_id,
        "status": redeem_req.status,
        "createdAt": redeem_req.created_at.isoformat(),
    }


@router.get("/wallet/redeem/pending")
async def get_pending_redeem_requests(session: AsyncSession = Depends(get_session)):
    """List redeem requests awaiting a manual UPI payout."""
    try:
        redeem_requests = await list_pending_redeems(session)
        return {
            "success": True,
            "redeemRequests": [_redeem_request_to_dict(r) for r in redeem_requests],
        }
    except Exception as e:
        logger.error(f"Error in get_pending_redeem_requests: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/wallet/redeem/{redeem_id}/mark-paid")
async def admin_mark_redeem_paid(
    redeem_id: str,
    admin: AdminUser = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
):
    """Confirm a UPI payout was sent (done manually today; this just closes out
    the reserved balance once you've actually paid the user)."""
    try:
        redeem_req = await mark_redeem_paid(session, parse_uuid(redeem_id), admin.email)
        logger.warning("Admin %s marked redeem %s paid", admin.email, redeem_id)
        return {
            "success": True,
            "message": "Redeem request marked paid",
            "redeemRequest": _redeem_request_to_dict(redeem_req),
        }
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in admin_mark_redeem_paid: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/wallet/redeem/{redeem_id}/reject")
async def admin_reject_redeem(
    redeem_id: str,
    action_data: AdminRedeemAction,
    admin: AdminUser = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
):
    """Reject a redeem request (e.g. invalid UPI ID) — releases the reserved
    balance back to the user's available balance."""
    try:
        redeem_req = await reject_redeem(
            session, parse_uuid(redeem_id), admin.email, action_data.reason
        )
        logger.warning("Admin %s rejected redeem %s: %s", admin.email, redeem_id, action_data.reason)
        return {
            "success": True,
            "message": "Redeem request rejected",
            "redeemRequest": _redeem_request_to_dict(redeem_req),
        }
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in admin_reject_redeem: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/notifications/send")
async def admin_send_notification_batch(
    request: SendNotificationBatchRequest,
    session: AsyncSession = Depends(get_session),
):
    """Ad-hoc push to a hand-picked list of users — bypasses the campaign
    engine/registry entirely, for one-off sends that don't warrant a
    registered NotificationJob (e.g. a marketing blast to a specific list)."""
    try:
        user_uuids = [parse_uuid(uid) for uid in request.userIds]
        tokens = await device_repository.get_tokens_for_user_ids(session, user_uuids)

        messages = [
            PreparedMessage(
                user_id=str(uid),
                token=tokens[str(uid)],
                title=request.title,
                body=request.body,
                data=request.data,
            )
            for uid in user_uuids
            if str(uid) in tokens
        ]
        no_token_count = len(user_uuids) - len(messages)

        sent_user_ids = send_batch(messages)
        for uid in sent_user_ids:
            notification_repository.record_sent(
                session, parse_uuid(uid), "manual", request.title, request.body
            )
        await session.commit()

        return {
            "success": True,
            "requested": len(user_uuids),
            "sent": len(sent_user_ids),
            "noPushToken": no_token_count,
            "failed": len(messages) - len(sent_user_ids),
        }
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in admin_send_notification_batch: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
