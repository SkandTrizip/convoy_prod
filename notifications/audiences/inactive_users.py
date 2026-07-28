from datetime import datetime, timedelta
from typing import List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import User, UserActivity
from notifications.audiences.base import AudienceBuilder

INACTIVE_AFTER_DAYS = 7


class InactiveUsersAudience(AudienceBuilder):
    """Users with no post or search activity in the last 7 days.

    A user with zero UserActivity rows ever is only included once they've had
    the account for at least INACTIVE_AFTER_DAYS — otherwise a brand-new signup
    with no activity yet would get flagged inactive on day one.
    """

    async def get_users(self, session: AsyncSession) -> List[User]:
        cutoff = datetime.utcnow() - timedelta(days=INACTIVE_AFTER_DAYS)
        last_activity = (
            select(UserActivity.user_id, func.max(UserActivity.created_at).label("last_at"))
            .group_by(UserActivity.user_id)
            .subquery()
        )
        result = await session.execute(
            select(User)
            .outerjoin(last_activity, User.id == last_activity.c.user_id)
            .where(
                User.account_status == "active",
                User.created_date < cutoff,
                (last_activity.c.last_at.is_(None)) | (last_activity.c.last_at < cutoff),
            )
        )
        return list(result.scalars().all())
