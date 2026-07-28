from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import User
from notifications.audiences.base import AudienceBuilder


class FestivalAudience(AudienceBuilder):
    """All active users — festival greetings aren't behavior-targeted. Which
    festival (if any) is active today is decided by the job/template, not here."""

    async def get_users(self, session: AsyncSession) -> List[User]:
        result = await session.execute(select(User).where(User.account_status == "active"))
        return list(result.scalars().all())
