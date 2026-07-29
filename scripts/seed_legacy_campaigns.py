#!/usr/bin/env python3
"""One-time migration: convert the old hardcoded morning/afternoon/night
campaign registry (notifications/registry.py) into real, DB-driven Campaign
rows, so the marketing team can finally see/edit/pause them through the admin
panel instead of them being invisible Python code.

Safe to re-run — skips any campaign whose name already exists.

Usage:
    python scripts/seed_legacy_campaigns.py
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from db import async_session, close_db, init_db  # noqa: E402
from db.base import Campaign, CampaignContent, CampaignDeliveryRule, CampaignSchedule  # noqa: E402
from services.campaigns.scheduling import compute_next_run_at  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


async def _campaign_exists(session, name: str) -> bool:
    result = await session.execute(select(Campaign.id).where(Campaign.name == name))
    return result.scalar_one_or_none() is not None


async def _seed_incomplete_kyc(session) -> None:
    name = "Incomplete KYC Reminder"
    if await _campaign_exists(session, name):
        print(f"Skipping '{name}' — already exists")
        return

    campaign = Campaign(
        name=name,
        description="Daily nudge for drivers who haven't finished KYC yet. "
        "Migrated from notifications/jobs/incomplete_kyc.py.",
        campaign_type="scheduled",
        status="scheduled",
        audience_filter={
            "combinator": "AND",
            "rules": [{"field": "kyc_status", "operator": "neq", "value": "approved"}],
        },
    )
    session.add(campaign)
    await session.flush()

    variants = [
        "Bas ek step aur.",
        "KYC complete karo.",
        "Verification ke baad loads milna shuru.",
        "Documents upload kar dijiye.",
        "Account activate karte hain.",
        "Truck list karne ke liye KYC zaroori hai.",
        "2 minutes ka kaam.",
        "KYC pending hai.",
        "Ready ho jao earning ke liye.",
        "Complete KYC today.",
    ]
    for i, body in enumerate(variants):
        session.add(
            CampaignContent(
                campaign_id=campaign.id,
                title="Complete Your KYC",
                body=body,
                data_payload={"type": "incomplete_kyc"},
                sort_order=i,
            )
        )

    now = datetime.utcnow()
    schedule = CampaignSchedule(
        campaign_id=campaign.id,
        schedule_type="daily",
        timezone="Asia/Kolkata",
        start_date=now,
        time_of_day="07:30",
        enabled=True,
    )
    schedule.next_run_at = compute_next_run_at(schedule, after=now)
    session.add(schedule)

    session.add(CampaignDeliveryRule(campaign_id=campaign.id, max_per_user_per_day=1))
    print(f"Seeded '{name}' — next run {schedule.next_run_at} UTC")


async def _seed_inactive_user(session) -> None:
    name = "Inactive User Win-back"
    if await _campaign_exists(session, name):
        print(f"Skipping '{name}' — already exists")
        return

    week_ago_iso = (datetime.utcnow()).isoformat()
    campaign = Campaign(
        name=name,
        description="Daily nudge for accounts >=7 days old with no recent search/post "
        "activity. Migrated from notifications/jobs/inactive_user.py "
        "(INACTIVE_AFTER_DAYS=7).",
        campaign_type="scheduled",
        status="scheduled",
        audience_filter={
            "combinator": "AND",
            "rules": [
                {"field": "registration_date", "operator": "before", "value": week_ago_iso},
                {"field": "last_active_date", "operator": "not_within_last_days", "value": 7},
            ],
        },
    )
    session.add(campaign)
    await session.flush()

    variants = [
        "Kaafi din ho gaye... Convoy miss kiya?",
        "Aaj phir se shuru karein?",
        "Truck list nahi kiya aaj.",
        "Naye loads wait kar rahe hain.",
        "Wapas aao Driver Ji.",
    ]
    for i, body in enumerate(variants):
        session.add(
            CampaignContent(
                campaign_id=campaign.id,
                title="We Miss You",
                body=body,
                data_payload={"type": "inactive_user"},
                sort_order=i,
            )
        )

    now = datetime.utcnow()
    schedule = CampaignSchedule(
        campaign_id=campaign.id,
        schedule_type="daily",
        timezone="Asia/Kolkata",
        start_date=now,
        time_of_day="13:30",
        enabled=True,
    )
    schedule.next_run_at = compute_next_run_at(schedule, after=now)
    session.add(schedule)

    session.add(CampaignDeliveryRule(campaign_id=campaign.id, max_per_user_per_day=1))
    print(f"Seeded '{name}' — next run {schedule.next_run_at} UTC")


async def _seed_independence_day(session) -> None:
    name = "Independence Day Greeting 2026"
    if await _campaign_exists(session, name):
        print(f"Skipping '{name}' — already exists")
        return

    campaign = Campaign(
        name=name,
        description="One-time festival greeting to all active users. Migrated from "
        "notifications/jobs/festival_greeting.py's hardcoded FESTIVAL_CALENDAR "
        "entry for (2026, 8, 15). Future festivals: just create another "
        "one-time campaign — no code change needed anymore.",
        campaign_type="scheduled",
        status="scheduled",
        audience_filter={},
    )
    session.add(campaign)
    await session.flush()

    variants = [
        "Bharat chal raha hai kyunki aap chal rahe ho.",
        "Har safar desh ki tarakki ka hissa hai.",
    ]
    for i, body in enumerate(variants):
        session.add(
            CampaignContent(
                campaign_id=campaign.id,
                title="Independence Day Wishes",
                body=body,
                data_payload={"type": "festival_greeting", "festival": "Independence Day"},
                sort_order=i,
            )
        )

    start_local = datetime(2026, 8, 15, 9, 0, tzinfo=IST)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    schedule = CampaignSchedule(
        campaign_id=campaign.id,
        schedule_type="one_time",
        timezone="Asia/Kolkata",
        start_date=start_utc,
        enabled=True,
        next_run_at=start_utc,
    )
    session.add(schedule)

    session.add(CampaignDeliveryRule(campaign_id=campaign.id, max_per_user_per_day=1))
    print(f"Seeded '{name}' — fires {start_utc} UTC (2026-08-15 09:00 IST)")


async def main() -> None:
    await init_db()
    try:
        async with async_session() as session:
            await _seed_incomplete_kyc(session)
            await _seed_inactive_user(session)
            await _seed_independence_day(session)
            await session.commit()
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
