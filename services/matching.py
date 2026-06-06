from datetime import datetime
from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from db.base import Notification, SearchDemand
from services.geo import calculate_distance
from services.notifications import send_expo_push_notification


async def process_smart_match_notifications(
    post_id: str, truck_post: Dict, session: AsyncSession
):
    """Process and send smart match notifications for a new truck post"""
    try:
        current_time = datetime.utcnow()

        result = await session.execute(
            select(SearchDemand).where(
                SearchDemand.truck_type == truck_post["truckType"],
                SearchDemand.expiry_timestamp > current_time,
                SearchDemand.notification_status != "sent",
            )
        )
        search_demands = result.scalars().all()

        for demand in search_demands:
            origin_distance = calculate_distance(
                demand.origin["lat"],
                demand.origin["lng"],
                truck_post["origin"]["lat"],
                truck_post["origin"]["lng"],
            )

            destination_distance = calculate_distance(
                demand.destination["lat"],
                demand.destination["lng"],
                truck_post["destination"]["lat"],
                truck_post["destination"]["lng"],
            )

            if origin_distance <= 100 and destination_distance <= 250:
                await send_expo_push_notification(
                    str(demand.user_id),
                    "Matching Truck Available",
                    f"A matching truck has been posted for your searched route from {truck_post['origin']['name']} to {truck_post['destination']['name']}",
                    {"postId": post_id, "type": "smart_match"},
                    session=session,
                )

                session.add(
                    Notification(
                        user_id=demand.user_id,
                        type="smart_match",
                        title="Matching Truck Available",
                        description="A matching truck has been posted for your searched route",
                        related_post_id=post_id,
                        created_at=datetime.utcnow(),
                        read_status=False,
                    )
                )

                demand.notification_status = "sent"

        await session.commit()
        logger.info(f"Processed smart match notifications for post {post_id}")
    except Exception as e:
        logger.error(f"Error in process_smart_match_notifications: {str(e)}")
