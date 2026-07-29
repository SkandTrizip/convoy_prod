"""Admin-facing Campaign Management System API. Mounted under /api/admin/campaigns.

Route ordering matters here: static paths (dashboard, executions,
filter-fields, audience-preview) are declared before the /{campaign_id}
paths so FastAPI doesn't try to parse "dashboard" etc. as a campaign UUID.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import (
    AdminUser,
    Campaign,
    CampaignContent,
    CampaignDeliveryRule,
    CampaignExecution,
    CampaignNotificationLog,
    CampaignSchedule,
)
from db.serializers import parse_uuid
from middleware.admin_auth import get_current_admin
from models import (
    AudiencePreviewRequest,
    CampaignContentInput,
    CampaignDeliveryRulesInput,
    CampaignRequest,
    CampaignScheduleInput,
    CampaignTestSendRequest,
)
from services.campaigns.audience_filters import (
    FILTER_REGISTRY,
    FilterError,
    build_audience_count_query,
    build_audience_query,
)
from services.campaigns.executor import run_campaign
from services.campaigns.scheduling import SUPPORTED_SCHEDULE_TYPES, compute_next_run_at

router = APIRouter(
    prefix="/admin/campaigns", tags=["admin-campaigns"], dependencies=[Depends(get_current_admin)]
)

UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _schedule_to_dict(schedule: CampaignSchedule | None) -> dict | None:
    if not schedule:
        return None
    return {
        "scheduleType": schedule.schedule_type,
        "timezone": schedule.timezone,
        "startDate": schedule.start_date,
        "endDate": schedule.end_date,
        "timeOfDay": schedule.time_of_day,
        "dayOfWeek": schedule.day_of_week,
        "dayOfMonth": schedule.day_of_month,
        "enabled": schedule.enabled,
        "nextRunAt": schedule.next_run_at,
        "lastRunAt": schedule.last_run_at,
    }


def _rules_to_dict(rules: CampaignDeliveryRule | None) -> dict | None:
    if not rules:
        return None
    return {
        "maxPerUserPerDay": rules.max_per_user_per_day,
        "minIntervalMinutes": rules.min_interval_minutes,
        "quietHoursStart": rules.quiet_hours_start,
        "quietHoursEnd": rules.quiet_hours_end,
        "respectPreferences": rules.respect_preferences,
    }


def _content_to_dict(content: CampaignContent) -> dict:
    return {
        "id": str(content.id),
        "title": content.title,
        "body": content.body,
        "dataPayload": content.data_payload,
        "sortOrder": content.sort_order,
    }


def _campaign_to_dict(
    campaign: Campaign,
    schedule: CampaignSchedule | None = None,
    rules: CampaignDeliveryRule | None = None,
    content_count: int | None = None,
) -> dict:
    return {
        "id": str(campaign.id),
        "name": campaign.name,
        "description": campaign.description,
        "campaignType": campaign.campaign_type,
        "status": campaign.status,
        "audienceFilter": campaign.audience_filter,
        "createdAt": campaign.created_at,
        "updatedAt": campaign.updated_at,
        "activatedAt": campaign.activated_at,
        "archivedAt": campaign.archived_at,
        "contentCount": content_count,
        "schedule": _schedule_to_dict(schedule),
        "deliveryRules": _rules_to_dict(rules),
    }


def _execution_to_dict(execution: CampaignExecution, campaign_name: str | None = None) -> dict:
    duration = None
    if execution.finished_at:
        duration = (execution.finished_at - execution.started_at).total_seconds()
    return {
        "id": str(execution.id),
        "campaignId": str(execution.campaign_id),
        "campaignName": campaign_name,
        "status": execution.status,
        "triggeredBy": execution.triggered_by,
        "startedAt": execution.started_at,
        "finishedAt": execution.finished_at,
        "durationSeconds": duration,
        "audienceSize": execution.audience_size,
        "sentCount": execution.sent_count,
        "failedCount": execution.failed_count,
        "noTokenCount": execution.no_token_count,
        "skippedCount": execution.skipped_count,
        "errorMessage": execution.error_message,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _local_to_utc(value: str | None, tz_name: str) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(UTC).replace(tzinfo=None)


async def _get_campaign_or_404(session: AsyncSession, campaign_uuid) -> Campaign:
    result = await session.execute(select(Campaign).where(Campaign.id == campaign_uuid))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _apply_contents(session: AsyncSession, campaign_id, contents: list[CampaignContentInput]) -> None:
    for i, item in enumerate(contents):
        session.add(
            CampaignContent(
                campaign_id=campaign_id,
                title=item.title,
                body=item.body,
                data_payload=item.dataPayload,
                sort_order=i,
            )
        )


def _apply_schedule(
    session: AsyncSession, campaign_id, schedule_input: CampaignScheduleInput | None
) -> CampaignSchedule | None:
    if not schedule_input:
        return None

    if schedule_input.scheduleType not in SUPPORTED_SCHEDULE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported scheduleType '{schedule_input.scheduleType}' "
            "(custom cron expressions are not available yet)",
        )
    if schedule_input.scheduleType in ("daily", "weekly", "monthly") and not schedule_input.timeOfDay:
        raise HTTPException(status_code=400, detail="timeOfDay is required for daily/weekly/monthly schedules")
    if schedule_input.scheduleType == "weekly" and schedule_input.dayOfWeek is None:
        raise HTTPException(status_code=400, detail="dayOfWeek is required for weekly schedules")
    if schedule_input.scheduleType == "monthly" and schedule_input.dayOfMonth is None:
        raise HTTPException(status_code=400, detail="dayOfMonth is required for monthly schedules")

    try:
        start_utc = _local_to_utc(schedule_input.startDate, schedule_input.timezone)
        end_utc = _local_to_utc(schedule_input.endDate, schedule_input.timezone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid schedule date: {e}") from e

    schedule = CampaignSchedule(
        campaign_id=campaign_id,
        schedule_type=schedule_input.scheduleType,
        timezone=schedule_input.timezone,
        start_date=start_utc,
        end_date=end_utc,
        time_of_day=schedule_input.timeOfDay,
        day_of_week=schedule_input.dayOfWeek,
        day_of_month=schedule_input.dayOfMonth,
        enabled=schedule_input.enabled,
    )
    schedule.next_run_at = (
        compute_next_run_at(schedule, after=datetime.utcnow()) if schedule_input.enabled else None
    )
    session.add(schedule)
    return schedule


def _apply_delivery_rules(
    session: AsyncSession, campaign_id, rules_input: CampaignDeliveryRulesInput | None
) -> CampaignDeliveryRule:
    rules_input = rules_input or CampaignDeliveryRulesInput()
    rules = CampaignDeliveryRule(
        campaign_id=campaign_id,
        max_per_user_per_day=rules_input.maxPerUserPerDay,
        min_interval_minutes=rules_input.minIntervalMinutes,
        quiet_hours_start=rules_input.quietHoursStart,
        quiet_hours_end=rules_input.quietHoursEnd,
        respect_preferences=rules_input.respectPreferences,
    )
    session.add(rules)
    return rules


async def _list_executions(session: AsyncSession, page: int, page_size: int, campaign_id: str | None) -> dict:
    stmt = (
        select(CampaignExecution, Campaign.name)
        .join(Campaign, Campaign.id == CampaignExecution.campaign_id)
        .order_by(CampaignExecution.started_at.desc())
    )
    count_stmt = select(func.count()).select_from(CampaignExecution)
    if campaign_id:
        cid = parse_uuid(campaign_id)
        stmt = stmt.where(CampaignExecution.campaign_id == cid)
        count_stmt = count_stmt.where(CampaignExecution.campaign_id == cid)

    total = await session.scalar(count_stmt)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).all()
    return {
        "success": True,
        "executions": [_execution_to_dict(ex, name) for ex, name in rows],
        "page": page,
        "pageSize": page_size,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Static routes (must precede /{campaign_id})
# ---------------------------------------------------------------------------

@router.get("")
async def list_campaigns(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    search: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    try:
        conditions = []
        if status:
            conditions.append(Campaign.status == status)
        if search and search.strip():
            conditions.append(Campaign.name.ilike(f"%{search.strip()}%"))

        count_stmt = select(func.count()).select_from(Campaign)
        list_stmt = select(Campaign).order_by(Campaign.created_at.desc())
        for c in conditions:
            count_stmt = count_stmt.where(c)
            list_stmt = list_stmt.where(c)

        total = await session.scalar(count_stmt)
        list_stmt = list_stmt.offset((page - 1) * pageSize).limit(pageSize)
        campaigns = list((await session.execute(list_stmt)).scalars().all())

        campaign_ids = [c.id for c in campaigns]
        schedules: dict = {}
        content_counts: dict = {}
        if campaign_ids:
            sched_result = await session.execute(
                select(CampaignSchedule).where(CampaignSchedule.campaign_id.in_(campaign_ids))
            )
            schedules = {s.campaign_id: s for s in sched_result.scalars().all()}

            count_result = await session.execute(
                select(CampaignContent.campaign_id, func.count())
                .where(CampaignContent.campaign_id.in_(campaign_ids))
                .group_by(CampaignContent.campaign_id)
            )
            content_counts = dict(count_result.all())

        items = [
            _campaign_to_dict(c, schedules.get(c.id), None, content_counts.get(c.id, 0)) for c in campaigns
        ]
        return {"success": True, "campaigns": items, "page": page, "pageSize": pageSize, "total": total}
    except Exception as e:
        logger.error(f"Error in list_campaigns: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
async def create_campaign(
    payload: CampaignRequest,
    admin: AdminUser = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        if payload.campaignType not in ("manual", "scheduled", "triggered"):
            raise HTTPException(status_code=400, detail="Invalid campaignType")

        campaign = Campaign(
            name=payload.name,
            description=payload.description,
            campaign_type=payload.campaignType,
            status="draft",
            audience_filter=payload.audienceFilter,
            created_by=admin.id,
        )
        session.add(campaign)
        await session.flush()

        _apply_contents(session, campaign.id, payload.contents)
        schedule = _apply_schedule(session, campaign.id, payload.schedule)
        rules = _apply_delivery_rules(session, campaign.id, payload.deliveryRules)

        await session.commit()
        return {
            "success": True,
            "campaign": _campaign_to_dict(campaign, schedule, rules, len(payload.contents)),
        }
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in create_campaign: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dashboard")
async def campaigns_dashboard(session: AsyncSession = Depends(get_session)):
    try:
        status_result = await session.execute(select(Campaign.status, func.count()).group_by(Campaign.status))
        status_counts = dict(status_result.all())

        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = await session.scalar(
            select(func.count())
            .select_from(CampaignNotificationLog)
            .where(
                CampaignNotificationLog.status == "sent",
                CampaignNotificationLog.sent_at >= today_start,
            )
        )
        total_sent = await session.scalar(
            select(func.count())
            .select_from(CampaignNotificationLog)
            .where(CampaignNotificationLog.status == "sent")
        )

        recent_result = await session.execute(
            select(CampaignExecution, Campaign.name)
            .join(Campaign, Campaign.id == CampaignExecution.campaign_id)
            .order_by(CampaignExecution.started_at.desc())
            .limit(5)
        )
        recent = [_execution_to_dict(ex, name) for ex, name in recent_result.all()]

        return {
            "success": True,
            "statusCounts": status_counts,
            "sentToday": sent_today or 0,
            "totalSent": total_sent or 0,
            "recentExecutions": recent,
        }
    except Exception as e:
        logger.error(f"Error in campaigns_dashboard: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/executions")
async def list_all_executions(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    campaignId: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await _list_executions(session, page, pageSize, campaignId)
    except Exception as e:
        logger.error(f"Error in list_all_executions: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/filter-fields")
async def get_filter_fields():
    fields = {
        key: {"label": defn["label"], "valueType": defn["value_type"], "operators": defn["operators"]}
        for key, defn in FILTER_REGISTRY.items()
    }
    return {"success": True, "fields": fields}


@router.post("/audience-preview")
async def preview_audience(payload: AudiencePreviewRequest, session: AsyncSession = Depends(get_session)):
    try:
        count = await session.scalar(build_audience_count_query(payload.audienceFilter))
        sample_result = await session.execute(
            build_audience_query(payload.audienceFilter).limit(payload.sampleSize)
        )
        sample_users = sample_result.scalars().all()
    except FilterError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error in preview_audience: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "success": True,
        "estimatedCount": count,
        "sampleUsers": [
            {"id": str(u.id), "name": u.name, "mobile": u.mobile, "kycStatus": u.kyc_status}
            for u in sample_users
        ],
    }


# ---------------------------------------------------------------------------
# /{campaign_id} routes
# ---------------------------------------------------------------------------

@router.get("/{campaign_id}")
async def get_campaign(campaign_id: str, session: AsyncSession = Depends(get_session)):
    try:
        campaign_uuid = parse_uuid(campaign_id)
        campaign = await _get_campaign_or_404(session, campaign_uuid)

        contents_result = await session.execute(
            select(CampaignContent)
            .where(CampaignContent.campaign_id == campaign_uuid)
            .order_by(CampaignContent.sort_order)
        )
        contents = list(contents_result.scalars().all())

        schedule_result = await session.execute(
            select(CampaignSchedule).where(CampaignSchedule.campaign_id == campaign_uuid)
        )
        schedule = schedule_result.scalar_one_or_none()

        rules_result = await session.execute(
            select(CampaignDeliveryRule).where(CampaignDeliveryRule.campaign_id == campaign_uuid)
        )
        rules = rules_result.scalar_one_or_none()

        data = _campaign_to_dict(campaign, schedule, rules, len(contents))
        data["contents"] = [_content_to_dict(c) for c in contents]
        return {"success": True, "campaign": data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_campaign: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{campaign_id}")
async def update_campaign(
    campaign_id: str, payload: CampaignRequest, session: AsyncSession = Depends(get_session)
):
    try:
        campaign_uuid = parse_uuid(campaign_id)
        campaign = await _get_campaign_or_404(session, campaign_uuid)
        if campaign.status == "archived":
            raise HTTPException(status_code=400, detail="Archived campaigns cannot be edited")

        campaign.name = payload.name
        campaign.description = payload.description
        campaign.campaign_type = payload.campaignType
        campaign.audience_filter = payload.audienceFilter
        campaign.updated_at = datetime.utcnow()

        await session.execute(delete(CampaignContent).where(CampaignContent.campaign_id == campaign_uuid))
        await session.execute(delete(CampaignSchedule).where(CampaignSchedule.campaign_id == campaign_uuid))
        await session.execute(
            delete(CampaignDeliveryRule).where(CampaignDeliveryRule.campaign_id == campaign_uuid)
        )

        _apply_contents(session, campaign_uuid, payload.contents)
        schedule = _apply_schedule(session, campaign_uuid, payload.schedule)
        rules = _apply_delivery_rules(session, campaign_uuid, payload.deliveryRules)

        await session.commit()
        return {
            "success": True,
            "campaign": _campaign_to_dict(campaign, schedule, rules, len(payload.contents)),
        }
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in update_campaign: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{campaign_id}")
async def delete_campaign(campaign_id: str, session: AsyncSession = Depends(get_session)):
    try:
        campaign_uuid = parse_uuid(campaign_id)
        campaign = await _get_campaign_or_404(session, campaign_uuid)
        if campaign.status in ("scheduled", "running", "paused"):
            raise HTTPException(
                status_code=400, detail="Pause and archive an active campaign before deleting it"
            )

        await session.execute(delete(Campaign).where(Campaign.id == campaign_uuid))
        await session.commit()
        return {"success": True, "message": "Campaign deleted"}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in delete_campaign: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{campaign_id}/duplicate")
async def duplicate_campaign(
    campaign_id: str,
    admin: AdminUser = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        campaign_uuid = parse_uuid(campaign_id)
        original = await _get_campaign_or_404(session, campaign_uuid)

        clone = Campaign(
            name=f"{original.name} (Copy)",
            description=original.description,
            campaign_type=original.campaign_type,
            status="draft",
            audience_filter=original.audience_filter,
            created_by=admin.id,
        )
        session.add(clone)
        await session.flush()

        contents_result = await session.execute(
            select(CampaignContent)
            .where(CampaignContent.campaign_id == campaign_uuid)
            .order_by(CampaignContent.sort_order)
        )
        for content in contents_result.scalars().all():
            session.add(
                CampaignContent(
                    campaign_id=clone.id,
                    title=content.title,
                    body=content.body,
                    data_payload=content.data_payload,
                    sort_order=content.sort_order,
                )
            )

        schedule_result = await session.execute(
            select(CampaignSchedule).where(CampaignSchedule.campaign_id == campaign_uuid)
        )
        original_schedule = schedule_result.scalar_one_or_none()
        if original_schedule:
            # Disabled on clone — an admin must explicitly re-activate a duplicate,
            # so two identical campaigns never both fire silently.
            session.add(
                CampaignSchedule(
                    campaign_id=clone.id,
                    schedule_type=original_schedule.schedule_type,
                    timezone=original_schedule.timezone,
                    start_date=original_schedule.start_date,
                    end_date=original_schedule.end_date,
                    time_of_day=original_schedule.time_of_day,
                    day_of_week=original_schedule.day_of_week,
                    day_of_month=original_schedule.day_of_month,
                    enabled=False,
                    next_run_at=None,
                )
            )

        rules_result = await session.execute(
            select(CampaignDeliveryRule).where(CampaignDeliveryRule.campaign_id == campaign_uuid)
        )
        original_rules = rules_result.scalar_one_or_none()
        if original_rules:
            session.add(
                CampaignDeliveryRule(
                    campaign_id=clone.id,
                    max_per_user_per_day=original_rules.max_per_user_per_day,
                    min_interval_minutes=original_rules.min_interval_minutes,
                    quiet_hours_start=original_rules.quiet_hours_start,
                    quiet_hours_end=original_rules.quiet_hours_end,
                    respect_preferences=original_rules.respect_preferences,
                )
            )

        await session.commit()
        return {"success": True, "campaignId": str(clone.id)}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in duplicate_campaign: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{campaign_id}/activate")
async def activate_campaign(campaign_id: str, session: AsyncSession = Depends(get_session)):
    try:
        campaign_uuid = parse_uuid(campaign_id)
        campaign = await _get_campaign_or_404(session, campaign_uuid)
        if campaign.status not in ("draft", "testing", "paused"):
            raise HTTPException(
                status_code=400, detail=f"Cannot activate a campaign in '{campaign.status}' status"
            )

        schedule_result = await session.execute(
            select(CampaignSchedule).where(CampaignSchedule.campaign_id == campaign_uuid)
        )
        schedule = schedule_result.scalar_one_or_none()

        if campaign.campaign_type == "manual" or (schedule and schedule.schedule_type == "immediate"):
            execution = await run_campaign(session, campaign, triggered_by="manual")
            campaign.status = "completed"
            campaign.activated_at = datetime.utcnow()
            await session.commit()
            return {
                "success": True,
                "campaign": _campaign_to_dict(campaign, schedule),
                "execution": _execution_to_dict(execution),
            }

        if not schedule:
            raise HTTPException(status_code=400, detail="Campaign has no schedule configured")

        schedule.enabled = True
        schedule.next_run_at = compute_next_run_at(schedule, after=datetime.utcnow())
        campaign.status = "scheduled"
        campaign.activated_at = datetime.utcnow()
        await session.commit()
        return {"success": True, "campaign": _campaign_to_dict(campaign, schedule)}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in activate_campaign: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{campaign_id}/pause")
async def pause_campaign(campaign_id: str, session: AsyncSession = Depends(get_session)):
    try:
        campaign_uuid = parse_uuid(campaign_id)
        campaign = await _get_campaign_or_404(session, campaign_uuid)
        if campaign.status not in ("scheduled", "running"):
            raise HTTPException(status_code=400, detail=f"Cannot pause a campaign in '{campaign.status}' status")

        await session.execute(
            update(CampaignSchedule).where(CampaignSchedule.campaign_id == campaign_uuid).values(enabled=False)
        )
        campaign.status = "paused"
        campaign.updated_at = datetime.utcnow()
        await session.commit()
        return {"success": True, "message": "Campaign paused"}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in pause_campaign: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{campaign_id}/resume")
async def resume_campaign(campaign_id: str, session: AsyncSession = Depends(get_session)):
    try:
        campaign_uuid = parse_uuid(campaign_id)
        campaign = await _get_campaign_or_404(session, campaign_uuid)
        if campaign.status != "paused":
            raise HTTPException(status_code=400, detail=f"Cannot resume a campaign in '{campaign.status}' status")

        schedule_result = await session.execute(
            select(CampaignSchedule).where(CampaignSchedule.campaign_id == campaign_uuid)
        )
        schedule = schedule_result.scalar_one_or_none()
        if not schedule:
            raise HTTPException(status_code=400, detail="Campaign has no schedule configured")

        schedule.enabled = True
        schedule.next_run_at = compute_next_run_at(schedule, after=datetime.utcnow())
        campaign.status = "scheduled"
        campaign.updated_at = datetime.utcnow()
        await session.commit()
        return {"success": True, "message": "Campaign resumed"}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in resume_campaign: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{campaign_id}/archive")
async def archive_campaign(campaign_id: str, session: AsyncSession = Depends(get_session)):
    try:
        campaign_uuid = parse_uuid(campaign_id)
        campaign = await _get_campaign_or_404(session, campaign_uuid)
        if campaign.status == "archived":
            raise HTTPException(status_code=400, detail="Campaign is already archived")

        await session.execute(
            update(CampaignSchedule).where(CampaignSchedule.campaign_id == campaign_uuid).values(enabled=False)
        )
        campaign.status = "archived"
        campaign.archived_at = datetime.utcnow()
        await session.commit()
        return {"success": True, "message": "Campaign archived"}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in archive_campaign: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{campaign_id}/test-send")
async def test_send_campaign(
    campaign_id: str, payload: CampaignTestSendRequest, session: AsyncSession = Depends(get_session)
):
    try:
        campaign_uuid = parse_uuid(campaign_id)
        campaign = await _get_campaign_or_404(session, campaign_uuid)
        execution = await run_campaign(
            session, campaign, triggered_by="test", override_user_ids=payload.userIds
        )
        return {"success": True, "execution": _execution_to_dict(execution)}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in test_send_campaign: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{campaign_id}/executions")
async def list_campaign_executions(
    campaign_id: str,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    try:
        await _get_campaign_or_404(session, parse_uuid(campaign_id))
        return await _list_executions(session, page, pageSize, campaign_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in list_campaign_executions: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{campaign_id}/analytics")
async def campaign_analytics(campaign_id: str, session: AsyncSession = Depends(get_session)):
    try:
        campaign_uuid = parse_uuid(campaign_id)
        campaign = await _get_campaign_or_404(session, campaign_uuid)

        schedule_result = await session.execute(
            select(CampaignSchedule).where(CampaignSchedule.campaign_id == campaign_uuid)
        )
        schedule = schedule_result.scalar_one_or_none()

        agg_result = await session.execute(
            select(
                func.count(CampaignExecution.id),
                func.sum(CampaignExecution.sent_count),
                func.sum(CampaignExecution.failed_count),
                func.sum(CampaignExecution.no_token_count),
                func.max(CampaignExecution.audience_size),
            ).where(CampaignExecution.campaign_id == campaign_uuid)
        )
        exec_count, total_sent, total_failed, total_no_token, latest_audience_size = agg_result.one()

        durations_result = await session.execute(
            select(CampaignExecution.started_at, CampaignExecution.finished_at).where(
                CampaignExecution.campaign_id == campaign_uuid, CampaignExecution.finished_at.isnot(None)
            )
        )
        durations = [(f - s).total_seconds() for s, f in durations_result.all()]
        avg_duration = sum(durations) / len(durations) if durations else None

        # Manual/test runs go through run_campaign() directly rather than the
        # scheduler tick, so schedule.last_run_at (only updated by the tick)
        # can be stale — fall back to the most recent execution's start time.
        last_execution_started_at = await session.scalar(
            select(func.max(CampaignExecution.started_at)).where(
                CampaignExecution.campaign_id == campaign_uuid, CampaignExecution.triggered_by != "test"
            )
        )
        last_run_at = schedule.last_run_at if schedule and schedule.last_run_at else last_execution_started_at

        return {
            "success": True,
            "analytics": {
                "status": campaign.status,
                "audienceSize": latest_audience_size or 0,
                "executionCount": exec_count or 0,
                "totalSent": total_sent or 0,
                "totalFailed": total_failed or 0,
                "totalNoToken": total_no_token or 0,
                "avgDurationSeconds": avg_duration,
                "lastRunAt": last_run_at,
                "nextRunAt": schedule.next_run_at if schedule else None,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in campaign_analytics: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
