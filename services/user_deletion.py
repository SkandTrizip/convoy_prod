from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import (
    Booking,
    CallLog,
    KYCRecord,
    LoginOTP,
    Notification,
    SearchDemand,
    Truck,
    TruckRoute,
    User,
    UserActivity,
)


async def delete_user_cascade(session: AsyncSession, user: User) -> dict[str, int]:
    """Permanently delete a user and everything tied to them.

    Most tables here reference user_id/truck_route_id as plain UUID columns with no
    real foreign key (only UserContact and TruckRouteDestination have DB-level
    ON DELETE CASCADE), so each one needs an explicit delete — the database won't
    clean these up on its own. Caller commits.
    """
    user_id = user.id
    own_route_ids = select(TruckRoute.id).where(TruckRoute.user_id == user_id)

    deleted = {
        "bookingsOnTheirPosts": (
            await session.execute(
                delete(Booking).where(Booking.truck_route_id.in_(own_route_ids))
            )
        ).rowcount,
        "ownBookings": (
            await session.execute(delete(Booking).where(Booking.user_id == user_id))
        ).rowcount,
        # Cascades to truck_route_destinations via ON DELETE CASCADE.
        "truckRoutes": (
            await session.execute(delete(TruckRoute).where(TruckRoute.user_id == user_id))
        ).rowcount,
        "vehicles": (
            await session.execute(delete(Truck).where(Truck.user_id == user_id))
        ).rowcount,
        "kycRecords": (
            await session.execute(delete(KYCRecord).where(KYCRecord.user_id == user_id))
        ).rowcount,
        "searchDemands": (
            await session.execute(delete(SearchDemand).where(SearchDemand.user_id == user_id))
        ).rowcount,
        "notifications": (
            await session.execute(delete(Notification).where(Notification.user_id == user_id))
        ).rowcount,
        "callLogs": (
            await session.execute(delete(CallLog).where(CallLog.user_id == user_id))
        ).rowcount,
        "loginOtps": (
            await session.execute(delete(LoginOTP).where(LoginOTP.mobile == user.mobile))
        ).rowcount,
        # "post"-type rows also cascade automatically once truck_routes are deleted above
        # (truck_route_id has ON DELETE CASCADE); "search"-type rows have no such FK and
        # need this explicit delete.
        "recentActivity": (
            await session.execute(delete(UserActivity).where(UserActivity.user_id == user_id))
        ).rowcount,
    }

    # Cascades to user_contacts via ON DELETE CASCADE.
    await session.execute(delete(User).where(User.id == user_id))

    return deleted
