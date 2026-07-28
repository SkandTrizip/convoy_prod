from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import User
from notifications.audiences.base import AudienceBuilder


class IncompleteKYCAudience(AudienceBuilder):
    """Users who have logged in (a User row only ever exists post-OTP-verify)
    but haven't completed KYC."""

    async def get_users(self, session: AsyncSession) -> List[User]:
        result = await session.execute(
            select(User).where(
                User.account_status == "active",
                User.kyc_status != "approved",
            )
        )
        return list(result.scalars().all())
