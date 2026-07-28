from datetime import date
from typing import Dict, List, Optional, Tuple

from db.base import User
from notifications.audiences.festivals import FestivalAudience
from notifications.jobs.base import NotificationJob
from notifications.templates import festival_template as template
from notifications.templates.base import pick_variant


class FestivalGreetingNotification(NotificationJob):
    """should_run() decides once per campaign run whether a festival is active
    today and caches it on the instance for build_message() to read — this
    assumes a single job instance isn't run concurrently with itself, which
    holds as long as campaigns process one job at a time (see campaigns/base.py)."""

    type = "festival_greeting"
    audience_builder = FestivalAudience()

    def __init__(self):
        self._active_festival: Optional[str] = None

    def should_run(self, today: date) -> bool:
        self._active_festival = template.get_active_festival(today)
        return self._active_festival is not None

    def build_message(
        self, user: User, recent_descriptions: List[str]
    ) -> Optional[Tuple[str, str, Dict]]:
        if not self._active_festival:
            return None
        variants = template.VARIANTS_BY_FESTIVAL.get(self._active_festival, [])
        if not variants:
            return None
        body = pick_variant(variants, recent_descriptions)
        return (
            f"{self._active_festival} Wishes",
            body,
            {"type": self.type, "festival": self._active_festival},
        )
