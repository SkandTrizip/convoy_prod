"""Computes CampaignSchedule.next_run_at. Stored/compared as naive UTC,
matching this codebase's universal `datetime.utcnow()` convention — only
`time_of_day`/`day_of_week`/`day_of_month` are interpreted in the campaign's
own `timezone` (they're recurring wall-clock triggers, recomputed to UTC
each cycle); `start_date`/`end_date` are absolute UTC instants.

DST transitions and month-end edge cases (day_of_month > 28) are not
precision-critical for a marketing scheduler, so this favours simplicity —
day_of_month is capped to 1-28 (enforced at the API layer) specifically to
sidestep "the 31st doesn't exist in February" entirely.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.base import CampaignSchedule

UTC = ZoneInfo("UTC")

SUPPORTED_SCHEDULE_TYPES = {"immediate", "one_time", "daily", "weekly", "monthly"}


def compute_next_run_at(schedule: CampaignSchedule, after: datetime) -> datetime | None:
    """`after` is naive UTC. Returns naive UTC, or None if there's no further
    occurrence (past end_date, or an unsupported/immediate schedule type)."""
    if schedule.schedule_type not in SUPPORTED_SCHEDULE_TYPES:
        return None
    if schedule.schedule_type == "immediate":
        return None

    tz = ZoneInfo(schedule.timezone or "Asia/Kolkata")

    if schedule.schedule_type == "one_time":
        candidate_utc = schedule.start_date
    else:
        after_local = after.replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)
        hour, minute = (int(p) for p in (schedule.time_of_day or "00:00").split(":"))

        if schedule.schedule_type == "daily":
            candidate_local = after_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate_local <= after_local:
                candidate_local += timedelta(days=1)

        elif schedule.schedule_type == "weekly":
            candidate_local = after_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            target_dow = schedule.day_of_week if schedule.day_of_week is not None else candidate_local.weekday()
            days_ahead = (target_dow - candidate_local.weekday()) % 7
            candidate_local += timedelta(days=days_ahead)
            if candidate_local <= after_local:
                candidate_local += timedelta(days=7)

        else:  # monthly
            day = min(schedule.day_of_month or 1, 28)
            candidate_local = after_local.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
            if candidate_local <= after_local:
                year = candidate_local.year + (1 if candidate_local.month == 12 else 0)
                month = 1 if candidate_local.month == 12 else candidate_local.month + 1
                candidate_local = candidate_local.replace(year=year, month=month, day=day)

        candidate_utc = candidate_local.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)
        if candidate_utc < schedule.start_date:
            candidate_utc = schedule.start_date

    if schedule.end_date and candidate_utc > schedule.end_date:
        return None

    return candidate_utc
