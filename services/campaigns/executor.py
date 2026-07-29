"""Campaign execution engine — resolves the audience, applies delivery
rules, picks each user's next rotated content variation, sends via FCM, and
records CampaignExecution + CampaignNotificationLog (the 'Notification
History' entity). Used by the scheduler tick (automatic runs) and by the
admin panel's 'Send Now' / 'Send Test' actions alike — only how the audience
is chosen differs (resolved from audience_filter, vs. an explicit override
list of user ids for test sends)."""
from datetime import datetime
from typing import List, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from db.base import (
    Campaign,
    CampaignContent,
    CampaignDeliveryRule,
    CampaignExecution,
    CampaignNotificationLog,
    CampaignSchedule,
)
from notifications.repositories import device_repository, notification_repository
from notifications.sender.firebase_sender import PreparedMessage, send_batch
from services.campaigns.audience_filters import build_audience_query
from services.campaigns.delivery_rules import filter_eligible_users
from services.campaigns.rotation import get_next_content_for_users


async def _campaign_timezone(session: AsyncSession, campaign_id: UUID) -> str:
    result = await session.execute(
        select(CampaignSchedule.timezone).where(CampaignSchedule.campaign_id == campaign_id)
    )
    tz = result.scalar_one_or_none()
    return tz or "Asia/Kolkata"


async def run_campaign(
    session: AsyncSession,
    campaign: Campaign,
    *,
    triggered_by: str = "schedule",
    override_user_ids: Optional[List[str]] = None,
) -> CampaignExecution:
    """override_user_ids bypasses audience resolution and delivery rules
    entirely — used for test sends, where the admin already hand-picked
    exactly who should get it."""
    execution = CampaignExecution(campaign_id=campaign.id, status="running", triggered_by=triggered_by)
    session.add(execution)
    await session.flush()

    try:
        contents_result = await session.execute(
            select(CampaignContent)
            .where(CampaignContent.campaign_id == campaign.id)
            .order_by(CampaignContent.sort_order)
        )
        contents = list(contents_result.scalars().all())
        if not contents:
            raise ValueError("Campaign has no content variations")

        if override_user_ids is not None:
            user_uuids = [UUID(uid) for uid in override_user_ids]
            audience_size = len(user_uuids)
            eligible_ids = user_uuids
            skipped = 0
        else:
            audience_result = await session.execute(build_audience_query(campaign.audience_filter))
            users = list(audience_result.scalars().all())
            audience_size = len(users)
            user_uuids = [u.id for u in users]

            rules_result = await session.execute(
                select(CampaignDeliveryRule).where(CampaignDeliveryRule.campaign_id == campaign.id)
            )
            rules = rules_result.scalar_one_or_none()

            tz = ZoneInfo(await _campaign_timezone(session, campaign.id))
            local_now = datetime.now(tz).replace(tzinfo=None)
            eligible_ids, skipped = await filter_eligible_users(session, rules, user_uuids, local_now)

        content_assignment = await get_next_content_for_users(session, campaign.id, eligible_ids, contents)
        devices_by_user = await device_repository.get_active_devices_for_user_ids(session, eligible_ids)

        # One PreparedMessage per (user, device) — a user with N active
        # devices gets N fanned-out sends of the same rotated content.
        messages: List[PreparedMessage] = []
        no_token_ids: List[UUID] = []
        for user_id in eligible_ids:
            user_devices = devices_by_user.get(str(user_id), [])
            if not user_devices:
                no_token_ids.append(user_id)
                continue
            content = content_assignment[user_id]
            for device in user_devices:
                messages.append(
                    PreparedMessage(
                        user_id=str(user_id),
                        device_id=device.device_id,
                        token=device.fcm_token,
                        title=content.title,
                        body=content.body,
                        data=content.data_payload,
                    )
                )

        results = send_batch(messages) if messages else []

        invalid_device_ids = [r.device_id for r in results if r.should_deactivate]
        if invalid_device_ids:
            await device_repository.deactivate_devices(session, invalid_device_ids)

        # User-level: reached if *any* of their devices got the message.
        # Device-level: raw per-message counts, kept alongside for engineering
        # visibility (a user with 2 devices, one delivered, counts as 1
        # user reached but 2 devices targeted / 1 device delivered).
        targeted_user_ids = {UUID(r.user_id) for r in results}
        reached_user_ids = {UUID(r.user_id) for r in results if r.success}
        failed_user_ids = targeted_user_ids - reached_user_ids

        for result in results:
            content = content_assignment[UUID(result.user_id)]
            session.add(
                CampaignNotificationLog(
                    campaign_id=campaign.id,
                    execution_id=execution.id,
                    content_id=content.id,
                    user_id=UUID(result.user_id),
                    status="sent" if result.success else "failed",
                )
            )

        # One in-app Notification per user reached, not per device — a user
        # shouldn't see duplicate feed entries just for owning 2 phones.
        for user_id in reached_user_ids:
            content = content_assignment[user_id]
            notification_repository.record_sent(
                session, user_id, f"campaign:{campaign.id}", content.title, content.body
            )

        for user_id in no_token_ids:
            session.add(
                CampaignNotificationLog(
                    campaign_id=campaign.id,
                    execution_id=execution.id,
                    content_id=None,
                    user_id=user_id,
                    status="no_token",
                )
            )

        execution.status = "completed"
        execution.audience_size = audience_size
        execution.sent_count = len(reached_user_ids)
        execution.failed_count = len(failed_user_ids)
        execution.no_token_count = len(no_token_ids)
        execution.skipped_count = skipped
        execution.devices_targeted = len(messages)
        execution.devices_delivered = sum(1 for r in results if r.success)
        execution.finished_at = datetime.utcnow()

    except Exception as e:
        logger.error("Campaign %s execution failed: %s", campaign.id, e, exc_info=True)
        execution.status = "failed"
        execution.error_message = str(e)
        execution.finished_at = datetime.utcnow()

    await session.commit()
    await session.refresh(execution)
    return execution
