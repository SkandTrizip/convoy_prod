from typing import Dict, List, Optional, Tuple

from db.base import User
from notifications.audiences.incomplete_kyc import IncompleteKYCAudience
from notifications.jobs.base import NotificationJob
from notifications.templates import incomplete_kyc_template as template
from notifications.templates.base import pick_variant


class IncompleteKYCNotification(NotificationJob):
    type = "incomplete_kyc"
    audience_builder = IncompleteKYCAudience()

    def build_message(
        self, user: User, recent_descriptions: List[str]
    ) -> Optional[Tuple[str, str, Dict]]:
        body = pick_variant(template.VARIANTS, recent_descriptions)
        return template.TITLE, body, {"type": self.type}
