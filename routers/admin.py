from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import CallLog, KYCRecord, Notification, SearchDemand, Truck, TruckRoute, User
from db.serializers import kyc_to_dict, parse_uuid
from models import AdminKYCAction
from services.notifications import send_expo_push_notification

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/kyc/pending")
async def get_pending_kyc(session: AsyncSession = Depends(get_session)):
    """Get all pending KYC requests"""
    try:
        result = await session.execute(
            select(KYCRecord)
            .where(KYCRecord.status == "under_review")
            .order_by(KYCRecord.submitted_date.desc())
            .limit(100)
        )
        kyc_records = result.scalars().all()

        records = []
        for record in kyc_records:
            record_dict = kyc_to_dict(record)
            user_result = await session.execute(
                select(User).where(User.id == record.user_id)
            )
            user = user_result.scalar_one_or_none()
            if user:
                record_dict["userName"] = user.name or "Unknown"
                record_dict["userMobile"] = user.mobile
            records.append(record_dict)

        return {"success": True, "records": records}
    except Exception as e:
        logger.error(f"Error in get_pending_kyc: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/kyc/action")
async def admin_kyc_action(
    action_data: AdminKYCAction,
    session: AsyncSession = Depends(get_session),
):
    """Approve or reject KYC"""
    try:
        if action_data.action not in ["approve", "reject"]:
            raise HTTPException(status_code=400, detail="Invalid action")

        status = "approved" if action_data.action == "approve" else "rejected"
        user_uuid = parse_uuid(action_data.userId)
        now = datetime.utcnow()

        await session.execute(
            update(KYCRecord)
            .where(KYCRecord.user_id == user_uuid)
            .values(
                status=status,
                reviewed_date=now,
                rejection_reason=action_data.reason
                if action_data.action == "reject"
                else None,
            )
        )
        await session.execute(
            update(User).where(User.id == user_uuid).values(kyc_status=status)
        )

        title = "KYC Approved" if status == "approved" else "KYC Rejected"
        body = (
            "Your KYC has been approved. You can now add vehicles."
            if status == "approved"
            else f"Your KYC has been rejected. Reason: {action_data.reason}"
        )

        session.add(
            Notification(
                user_id=user_uuid,
                type="kyc_update",
                title=title,
                description=body,
                created_at=now,
                read_status=False,
            )
        )
        await session.commit()

        await send_expo_push_notification(
            action_data.userId, title, body, session=session
        )

        return {"success": True, "message": f"KYC {status}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in admin_kyc_action: {str(e)}")
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
