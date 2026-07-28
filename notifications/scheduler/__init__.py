"""Decides *when* each campaign runs. Doesn't know what a campaign sends or
to whom — that's campaigns/ and audiences/'s job."""
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import logger
from notifications.scheduler.afternoon import run_afternoon_campaign
from notifications.scheduler.morning import run_morning_campaign
from notifications.scheduler.night import run_night_campaign

TIMEZONE = "Asia/Kolkata"

_scheduler: Optional[AsyncIOScheduler] = None


def start_notification_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        run_morning_campaign, CronTrigger(hour=7, minute=30, timezone=TIMEZONE), id="morning_campaign"
    )
    scheduler.add_job(
        run_afternoon_campaign, CronTrigger(hour=13, minute=30, timezone=TIMEZONE), id="afternoon_campaign"
    )
    scheduler.add_job(
        run_night_campaign, CronTrigger(hour=21, minute=0, timezone=TIMEZONE), id="night_campaign"
    )
    scheduler.start()
    logger.info("Notification campaign scheduler started (morning 07:30, afternoon 13:30, night 21:00 IST)")

    _scheduler = scheduler
    return scheduler


def stop_notification_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
