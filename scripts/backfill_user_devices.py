#!/usr/bin/env python3
"""One-time migration: convert every user's legacy single `push_token` column
into a UserDevice row, so the new per-device send paths keep delivering to
existing users from day one — before the legacy shim in
routers/users.py:/push-token starts writing "legacy-*" rows on its own.

Safe to re-run — upsert_device is idempotent per device_id.

Usage:
    python scripts/backfill_user_devices.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from db import async_session, close_db, init_db  # noqa: E402
from db.base import User  # noqa: E402
from notifications.repositories import device_repository  # noqa: E402


async def main() -> None:
    await init_db()
    try:
        async with async_session() as session:
            result = await session.execute(
                select(User.id, User.push_token).where(User.push_token.isnot(None))
            )
            rows = result.all()

            for user_id, push_token in rows:
                await device_repository.upsert_device(
                    session,
                    user_id=user_id,
                    device_id=f"legacy-{user_id}",
                    platform="unknown",
                    fcm_token=push_token,
                )
            await session.commit()
            print(f"Backfilled {len(rows)} legacy device row(s) from users.push_token")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
