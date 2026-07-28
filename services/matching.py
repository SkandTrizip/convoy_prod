from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from db.base import Notification, SearchDemand, TruckRoute, TruckRouteDestination
from services.geo import calculate_distance
from services.notifications import send_push_notification


async def process_smart_match_notifications(
    session: AsyncSession,
    route: TruckRoute,
    destinations: Iterable[TruckRouteDestination],
) -> None:
    """Notify users whose saved search (SearchDemand) this new/reactivated/edited
    post now matches. Best-effort — never raises, so a failure here can't block
    post creation/reactivation/edit."""
    try:
        now = datetime.utcnow()
        result = await session.execute(
            select(SearchDemand).where(
                SearchDemand.expiry_timestamp > now,
                SearchDemand.notification_status != "sent",
            )
        )
        demands = result.scalars().all()
        if not demands:
            return

        origin_lat, origin_lng = route.origin["lat"], route.origin["lng"]
        dest_points = [
            (d.destination["lat"], d.destination["lng"], d.destination.get("name"))
            for d in destinations
        ]

        for demand in demands:
            if demand.truck_type and demand.truck_type != route.truck_type:
                continue
            if demand.capacity is not None and (
                route.capacity is None or route.capacity < demand.capacity
            ):
                continue

            origin_distance = calculate_distance(
                demand.origin["lat"], demand.origin["lng"], origin_lat, origin_lng
            )
            if origin_distance > demand.radius_km:
                continue

            matched_destination_name = next(
                (
                    name
                    for (lat, lng, name) in dest_points
                    if calculate_distance(
                        demand.destination["lat"], demand.destination["lng"], lat, lng
                    )
                    <= demand.radius_km
                ),
                None,
            )
            if matched_destination_name is None:
                continue

            already_notified = await session.execute(
                select(Notification).where(
                    Notification.user_id == demand.user_id,
                    Notification.type == "smart_match",
                    Notification.related_post_id == str(route.id),
                )
            )
            if already_notified.scalar_one_or_none():
                continue

            message = (
                f"A matching truck has been posted for your searched route "
                f"from {route.origin_name} to {matched_destination_name}"
            )
            await send_push_notification(
                str(demand.user_id),
                "Matching Truck Available",
                message,
                {"postId": str(route.id), "type": "smart_match"},
                session=session,
            )
            session.add(
                Notification(
                    user_id=demand.user_id,
                    type="smart_match",
                    title="Matching Truck Available",
                    description=message,
                    related_post_id=str(route.id),
                    created_at=now,
                    read_status=False,
                )
            )
            demand.notification_status = "sent"

        await session.commit()
        logger.info(f"Processed smart match notifications for post {route.id}")
    except Exception as e:
        logger.error(f"Error in process_smart_match_notifications: {str(e)}")
