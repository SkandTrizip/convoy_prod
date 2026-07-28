from abc import ABC, abstractmethod
from typing import List

from sqlalchemy.ext.asyncio import AsyncSession

from db.base import User


class AudienceBuilder(ABC):
    """Each notification type owns its audience independently — no shared query,
    no campaign-level filtering. Add a new builder here to back a new notification
    type; nothing else in the engine needs to change."""

    @abstractmethod
    async def get_users(self, session: AsyncSession) -> List[User]:
        ...
