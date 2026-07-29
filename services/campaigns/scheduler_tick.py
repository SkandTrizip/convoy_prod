"""Poll-based scheduler for data-driven campaigns. A single job runs every
minute and asks the DB which campaigns are due — rather than one APScheduler
job registered per campaign, which wouldn't survive a process restart (no
persistent job store is configured; see notifications/scheduler/__init__.py).
This tick is restart-safe by construction: it just re-reads
campaign_schedules.next_run_at on the next minute after a restart."""
from datetime import datetime

from sqlalchemy import select

from config import logger
from db import async_session
from db.base import Campaign, CampaignSchedule
from services.campaigns.executor import run_campaign
from services.campaigns.scheduling import compute_next_run_at


async def run_due_campaigns() -> None:
    try:
        async with async_session() as session:
            now = datetime.utcnow()
            result = await session.execute(
                select(Campaign, CampaignSchedule)
                .join(CampaignSchedule, CampaignSchedule.campaign_id == Campaign.id)
                .where(
                    Campaign.status.in_(("scheduled", "running")),
                    CampaignSchedule.enabled.is_(True),
                    CampaignSchedule.next_run_at.isnot(None),
                    CampaignSchedule.next_run_at <= now,
                )
            )
            due = result.all()

            for campaign, schedule in due:
                logger.info("Running due campaign %s (%s)", campaign.id, campaign.name)
                await run_campaign(session, campaign, triggered_by="schedule")

                schedule.last_run_at = now
                if schedule.schedule_type == "one_time":
                    schedule.enabled = False
                    schedule.next_run_at = None
                    campaign.status = "completed"
                else:
                    schedule.next_run_at = compute_next_run_at(schedule, after=now)
                    if campaign.status == "scheduled":
                        campaign.status = "running"
                campaign.updated_at = now
                await session.commit()
    except Exception as e:
        logger.error("Campaign scheduler tick failed: %s", e, exc_info=True)
