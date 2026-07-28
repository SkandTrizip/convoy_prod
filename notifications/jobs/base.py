from abc import ABC, abstractmethod
from datetime import date
from typing import Dict, List, Optional, Tuple

from db.base import User
from notifications.audiences.base import AudienceBuilder


class NotificationJob(ABC):
    """One notification type: pairs an audience with the copy it sends.
    Registered under one or more campaigns in notifications/registry.py — add
    a new job class + register it there to introduce a new notification type,
    nothing else in the engine needs to change."""

    type: str  # stable key — stored as Notification.type, used for rotation lookups
    audience_builder: AudienceBuilder

    def should_run(self, today: date) -> bool:
        """Gate checked once, before the (possibly expensive) audience query
        runs. Override for date-gated jobs like festival greetings."""
        return True

    @abstractmethod
    def build_message(
        self, user: User, recent_descriptions: List[str]
    ) -> Optional[Tuple[str, str, Dict]]:
        """Return (title, body, data) for this user, or None to skip them."""
        ...
