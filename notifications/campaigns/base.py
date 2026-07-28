from datetime import date, datetime
from typing import Dict, List, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from notifications.jobs.base import NotificationJob
from notifications.registry import CAMPAIGN_REGISTRY
from notifications.repositories import device_repository, notification_repository
from notifications.sender.firebase_sender import PreparedMessage, send_batch


class Campaign:
    """Runs every NotificationJob registered for `campaign_key` in
    notifications/registry.py. The campaign never queries the DB for audience
    directly — that's each job's audience_builder's responsibility."""

    campaign_key: str

    async def run(self, session: AsyncSession) -> None:
        jobs: List[NotificationJob] = CAMPAIGN_REGISTRY.get(self.campaign_key, [])
        if not jobs:
            logger.info(f"[{self.campaign_key}] campaign has no registered jobs, skipping")
            return

        today = datetime.utcnow().date()
        for job in jobs:
            await self._run_job(session, job, today)

    async def _run_job(self, session: AsyncSession, job: NotificationJob, today: date) -> None:
        if not job.should_run(today):
            logger.info(f"[{self.campaign_key}/{job.type}] should_run() False, skipping")
            return

        users = await job.audience_builder.get_users(session)
        if not users:
            logger.info(f"[{self.campaign_key}/{job.type}] audience empty, skipping")
            return

        user_ids = [user.id for user in users]
        recent = await notification_repository.get_recent_descriptions(
            session, user_ids, job.type
        )
        tokens = await device_repository.get_tokens_for_user_ids(session, user_ids)

        messages: List[PreparedMessage] = []
        built: Dict[str, Tuple] = {}
        for user in users:
            token = tokens.get(str(user.id))
            if not token:
                continue
            result = job.build_message(user, recent.get(str(user.id), []))
            if result is None:
                continue
            title, body, data = result
            messages.append(
                PreparedMessage(user_id=str(user.id), token=token, title=title, body=body, data=data)
            )
            built[str(user.id)] = (user.id, title, body)

        if not messages:
            logger.info(f"[{self.campaign_key}/{job.type}] nobody eligible had a push token, skipping")
            return

        sent_user_ids = send_batch(messages)
        for uid in sent_user_ids:
            user_id, title, body = built[uid]
            notification_repository.record_sent(session, user_id, job.type, title, body)
        await session.commit()

        logger.info(f"[{self.campaign_key}/{job.type}] sent {len(sent_user_ids)}/{len(messages)}")
