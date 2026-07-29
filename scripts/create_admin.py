#!/usr/bin/env python3
"""Create an admin user — needed once to seed the very first admin, since
after that, admins create admins via POST /api/admin-auth/admins.

Usage:
    python scripts/create_admin.py --email admin@convoy.app --password 'secret' [--name "Ankit"]
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import async_session, close_db, init_db  # noqa: E402
from services.admin_users import create_admin_user, get_admin_by_email  # noqa: E402


async def main(email: str, password: str, name: str | None) -> None:
    await init_db()
    try:
        async with async_session() as session:
            existing = await get_admin_by_email(session, email)
            if existing:
                print(f"Admin with email {email} already exists (id={existing.id})")
                return
            admin = await create_admin_user(session, email, password, name)
            print(f"Created admin {admin.email} (id={admin.id})")
    finally:
        await close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create an admin user")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--name", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.email, args.password, args.name))
