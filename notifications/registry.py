"""Declarative map from campaign -> the notification jobs it runs.

To add a new notification type: write a NotificationJob subclass under
notifications/jobs/ (backed by an AudienceBuilder + a template), then add an
instance of it to the campaign(s) below. No changes needed to the scheduler,
campaign base class, or sender.
"""
from notifications.jobs.festival_greeting import FestivalGreetingNotification
from notifications.jobs.inactive_user import InactiveUserNotification
from notifications.jobs.incomplete_kyc import IncompleteKYCNotification

CAMPAIGN_REGISTRY = {
    "morning": [
        IncompleteKYCNotification(),
        FestivalGreetingNotification(),
    ],
    "afternoon": [
        InactiveUserNotification(),
    ],
    "night": [
        # No jobs registered yet. Add a NotificationJob instance here — the
        # night campaign already runs and will pick it up automatically.
    ],
}
