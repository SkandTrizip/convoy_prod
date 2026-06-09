import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import User, UserContact

SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")
MAX_CONTACTS_PER_SYNC = 10_000
MUTUAL_PREVIEW_LIMIT = 5


def _normalize_hashed_number(value: str) -> str:
    cleaned = value.strip().lower()
    if not SHA256_HEX_RE.match(cleaned):
        raise ValueError("hashedNumber must be a 64-character SHA256 hex string")
    return cleaned


def _normalize_contact_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("Contact name is required")
    return name[:255]


def _dedupe_contacts(contacts: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for contact in contacts:
        hashed = contact["hashedNumber"]
        if hashed in seen:
            continue
        seen.add(hashed)
        unique.append(contact)
    return unique


async def sync_user_contacts(
    session: AsyncSession,
    user_id: uuid.UUID,
    contacts: list[dict[str, str]],
) -> dict[str, Any]:
    """Replace a user's hashed phone contacts (raw numbers never stored)."""
    if len(contacts) > MAX_CONTACTS_PER_SYNC:
        raise ValueError(f"Too many contacts (max {MAX_CONTACTS_PER_SYNC})")

    normalized = _dedupe_contacts(
        [
            {
                "name": _normalize_contact_name(item["name"]),
                "hashedNumber": _normalize_hashed_number(item["hashedNumber"]),
            }
            for item in contacts
        ]
    )

    await session.execute(delete(UserContact).where(UserContact.user_id == user_id))

    if normalized:
        session.add_all(
            [
                UserContact(
                    user_id=user_id,
                    hashed_number=item["hashedNumber"],
                    name=item["name"],
                )
                for item in normalized
            ]
        )

    now = datetime.utcnow()
    user_result = await session.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if user:
        user.contacts_last_updated = now

    await session.commit()

    return {
        "syncedCount": len(normalized),
        "contactsLastUpdated": now.isoformat(),
    }


async def find_mutual_contacts(
    session: AsyncSession,
    viewer_user_id: uuid.UUID,
    other_user_id: uuid.UUID,
) -> list[dict[str, str]]:
    """Contacts present in both users' phone books (matched by hashed number)."""
    if viewer_user_id == other_user_id:
        return []

    viewer_alias = UserContact.__table__.alias("viewer_contacts")
    other_alias = UserContact.__table__.alias("other_contacts")

    result = await session.execute(
        select(viewer_alias.c.name, viewer_alias.c.hashed_number)
        .select_from(
            viewer_alias.join(
                other_alias,
                viewer_alias.c.hashed_number == other_alias.c.hashed_number,
            )
        )
        .where(
            viewer_alias.c.user_id == viewer_user_id,
            other_alias.c.user_id == other_user_id,
        )
        .order_by(viewer_alias.c.name)
    )

    return [
        {"name": row.name, "hashedNumber": row.hashed_number}
        for row in result.all()
    ]


async def find_mutual_contacts_batch(
    session: AsyncSession,
    viewer_user_id: uuid.UUID,
    other_user_ids: list[uuid.UUID],
) -> dict[str, list[dict[str, str]]]:
    """Mutual contacts between viewer and many listing owners."""
    unique_other_ids = list({uid for uid in other_user_ids if uid != viewer_user_id})
    if not unique_other_ids:
        return {}

    viewer_alias = UserContact.__table__.alias("viewer_contacts")
    other_alias = UserContact.__table__.alias("other_contacts")

    result = await session.execute(
        select(
            other_alias.c.user_id,
            viewer_alias.c.name,
            viewer_alias.c.hashed_number,
        )
        .select_from(
            viewer_alias.join(
                other_alias,
                viewer_alias.c.hashed_number == other_alias.c.hashed_number,
            )
        )
        .where(
            viewer_alias.c.user_id == viewer_user_id,
            other_alias.c.user_id.in_(unique_other_ids),
        )
        .order_by(other_alias.c.user_id, viewer_alias.c.name)
    )

    grouped: dict[str, list[dict[str, str]]] = {}
    for row in result.all():
        owner_id = str(row.user_id)
        grouped.setdefault(owner_id, []).append(
            {"name": row.name, "hashedNumber": row.hashed_number}
        )
    return grouped


def mutual_preview(mutuals: list[dict[str, str]]) -> dict[str, Any]:
    """Compact mutual summary for truck listing cards."""
    return {
        "count": len(mutuals),
        "names": [m["name"] for m in mutuals[:MUTUAL_PREVIEW_LIMIT]],
    }


async def attach_mutuals_to_listings(
    session: AsyncSession,
    viewer_user_id: uuid.UUID,
    listings: list[dict[str, Any]],
    *,
    owner_id_key: str = "userId",
) -> list[dict[str, Any]]:
    """Add mutual contact summary to each listing for the viewing user."""
    owner_ids: list[uuid.UUID] = []
    for item in listings:
        raw_id = item.get(owner_id_key)
        if not raw_id:
            continue
        try:
            owner_ids.append(uuid.UUID(str(raw_id)))
        except ValueError:
            continue

    mutuals_by_owner = await find_mutual_contacts_batch(
        session, viewer_user_id, owner_ids
    )

    enriched: list[dict[str, Any]] = []
    for item in listings:
        owner_id = str(item.get(owner_id_key, ""))
        mutuals = mutuals_by_owner.get(owner_id, [])
        enriched.append(
            {
                **item,
                "mutuals": mutual_preview(mutuals),
            }
        )
    return enriched
