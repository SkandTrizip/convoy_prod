from typing import Dict, List, Optional, Tuple

from db.base import User
from notifications.audiences.inactive_users import InactiveUsersAudience
from notifications.jobs.base import NotificationJob
from notifications.templates import inactive_template as template
from notifications.templates.base import pick_variant


class InactiveUserNotification(NotificationJob):
    type = "inactive_user"
    audience_builder = InactiveUsersAudience()

    def build_message(
        self, user: User, recent_descriptions: List[str]
    ) -> Optional[Tuple[str, str, Dict]]:
        body = pick_variant(template.VARIANTS, recent_descriptions)
        return template.TITLE, body, {"type": self.type}
