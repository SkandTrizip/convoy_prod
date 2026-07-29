"""Delivery rule enforcement — filters a resolved audience down to users
eligible to receive a notification right now, per the campaign's
CampaignDeliveryRule.

Scope note: max_per_user_per_day and min_interval are enforced per-campaign
(counting only this campaign's own CampaignNotificationLog rows), not
globally across all campaigns a user might be enrolled in. Preventing
cross-campaign notification fatigue would need a different, global rule —
not what this table models.

respect_preferences is accepted but not enforced — there is no per-user
notification-preference model in this codebase yet.
"""
from datetime import datetime, timedelta
from typing import List, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import CampaignDeliveryRule, CampaignNotificationLog


def _in_quiet_hours(now_local: datetime, start: str | None, end: str | None) -> bool:
    if not start or not end:
        return False
    current = now_local.strftime("%H:%M")
    if start <= end:
        return start <= current < end
    return current >= start or current < end  # overnight window, e.g. 22:00-07:00


async def filter_eligible_users(
    session: AsyncSession,
    rules: CampaignDeliveryRule | None,
    user_ids: List[UUID],
    now_local: datetime,
) -> Tuple[List[UUID], int]:
    """`now_local` must already be in the campaign's configured timezone (naive).
    Returns (eligible_user_ids, skipped_count)."""
    if not rules or not user_ids:
        return list(user_ids), 0

    if _in_quiet_hours(now_local, rules.quiet_hours_start, rules.quiet_hours_end):
        return [], len(user_ids)

    since_start_of_day = now_local - timedelta(hours=24)
    sent_today_result = await session.execute(
        select(CampaignNotificationLog.user_id, func.count())
        .where(
            CampaignNotificationLog.campaign_id == rules.campaign_id,
            CampaignNotificationLog.user_id.in_(user_ids),
            CampaignNotificationLog.status == "sent",
            CampaignNotificationLog.sent_at >= since_start_of_day,
        )
        .group_by(CampaignNotificationLog.user_id)
    )
    sent_today = dict(sent_today_result.all())

    last_sent_result = await session.execute(
        select(CampaignNotificationLog.user_id, func.max(CampaignNotificationLog.sent_at))
        .where(
            CampaignNotificationLog.campaign_id == rules.campaign_id,
            CampaignNotificationLog.user_id.in_(user_ids),
            CampaignNotificationLog.status == "sent",
        )
        .group_by(CampaignNotificationLog.user_id)
    )
    last_sent = dict(last_sent_result.all())

    min_interval = timedelta(minutes=rules.min_interval_minutes or 0)
    eligible: List[UUID] = []
    skipped = 0
    for user_id in user_ids:
        if sent_today.get(user_id, 0) >= rules.max_per_user_per_day:
            skipped += 1
            continue
        last = last_sent.get(user_id)
        if last and min_interval and (now_local - last) < min_interval:
            skipped += 1
            continue
        eligible.append(user_id)

    return eligible, skipped
