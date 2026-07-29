"""Content rotation — a user must see every CampaignContent variation once
before any repeats. Derived entirely from CampaignNotificationLog (no
separate cursor table): for each user, the next content is the
lowest-sort_order variation they haven't been sent yet; once they've seen
every variation, the cycle resets and starts again from the top. Robust to
the content pool being edited later (deleted variations are ignored, newly
added ones show up as unseen immediately)."""
from collections import defaultdict
from typing import Dict, List
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import CampaignContent, CampaignNotificationLog


async def get_next_content_for_users(
    session: AsyncSession,
    campaign_id: UUID,
    user_ids: List[UUID],
    contents: List[CampaignContent],
) -> Dict[UUID, CampaignContent]:
    """Returns {user_id: CampaignContent} — the next variation to send each user."""
    if not contents or not user_ids:
        return {}

    ordered = sorted(contents, key=lambda c: c.sort_order)
    content_by_id = {c.id: c for c in ordered}
    content_order = [c.id for c in ordered]

    result = await session.execute(
        select(CampaignNotificationLog.user_id, CampaignNotificationLog.content_id).where(
            CampaignNotificationLog.campaign_id == campaign_id,
            CampaignNotificationLog.user_id.in_(user_ids),
            CampaignNotificationLog.status == "sent",
        )
    )
    seen: Dict[UUID, set] = defaultdict(set)
    for user_id, content_id in result.all():
        if content_id is not None:
            seen[user_id].add(content_id)

    assignment: Dict[UUID, CampaignContent] = {}
    for user_id in user_ids:
        unseen = [cid for cid in content_order if cid not in seen.get(user_id, set())]
        next_id = unseen[0] if unseen else content_order[0]
        assignment[user_id] = content_by_id[next_id]

    return assignment
