"""DB access for the Notification table, used by the campaign engine.

Kept separate from services/notifications.py, which owns the one-off
event-driven send path (KYC approval, smart match, etc)."""
import uuid
from datetime import datetime
from typing import Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import Notification


async def get_recent_descriptions(
    session: AsyncSession,
    user_ids: List[uuid.UUID],
    notification_type: str,
    limit: int = 3,
) -> Dict[str, List[str]]:
    """Last `limit` notification bodies sent to each user for this type, so
    template pickers can avoid repeating the same copy the user just saw."""
    if not user_ids:
        return {}

    row_number = (
        func.row_number()
        .over(partition_by=Notification.user_id, order_by=Notification.created_at.desc())
        .label("rn")
    )
    subquery = (
        select(Notification.user_id, Notification.description, row_number)
        .where(
            Notification.user_id.in_(user_ids),
            Notification.type == notification_type,
        )
        .subquery()
    )
    result = await session.execute(
        select(subquery.c.user_id, subquery.c.description).where(subquery.c.rn <= limit)
    )

    recent: Dict[str, List[str]] = {}
    for user_id, description in result.all():
        recent.setdefault(str(user_id), []).append(description)
    return recent


def record_sent(
    session: AsyncSession,
    user_id: uuid.UUID,
    notification_type: str,
    title: str,
    description: str,
) -> None:
    """Queue an in-app Notification row. Caller commits once per batch."""
    session.add(
        Notification(
            user_id=user_id,
            type=notification_type,
            title=title,
            description=description,
            created_at=datetime.utcnow(),
            read_status=False,
        )
    )
