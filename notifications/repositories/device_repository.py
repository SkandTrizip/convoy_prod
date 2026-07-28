"""Push-token lookups, used by the campaign engine and the ad-hoc admin send API."""
import uuid
from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import User


async def get_tokens_for_user_ids(
    session: AsyncSession, user_ids: List[uuid.UUID]
) -> Dict[str, str]:
    """user_id (str) -> push_token, for users that have one registered."""
    if not user_ids:
        return {}

    result = await session.execute(
        select(User.id, User.push_token).where(
            User.id.in_(user_ids), User.push_token.isnot(None)
        )
    )
    return {str(user_id): token for user_id, token in result.all()}
