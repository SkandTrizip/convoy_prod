"""Runs the campaign poll tick every minute — see
services/campaigns/scheduler_tick.py for what "due" means and what running a
campaign actually does. This file used to register three hardcoded
morning/afternoon/night cron jobs directly; those campaigns are now regular,
admin-editable Campaign rows (see scripts/seed_legacy_campaigns.py), so a
single generic tick replaces all three (and everything created after them)."""
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import logger
from services.campaigns.scheduler_tick import run_due_campaigns

TIMEZONE = "Asia/Kolkata"

_scheduler: Optional[AsyncIOScheduler] = None


def start_notification_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        run_due_campaigns,
        IntervalTrigger(minutes=1),
        id="campaign_scheduler_tick",
        max_instances=1,
    )
    scheduler.start()
    logger.info("Campaign scheduler tick started (every minute)")

    _scheduler = scheduler
    return scheduler


def stop_notification_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
