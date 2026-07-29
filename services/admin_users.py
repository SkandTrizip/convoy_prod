from typing import Optional

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import AdminUser


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


async def get_admin_by_email(session: AsyncSession, email: str) -> Optional[AdminUser]:
    result = await session.execute(select(AdminUser).where(AdminUser.email == email.lower()))
    return result.scalar_one_or_none()


async def create_admin_user(
    session: AsyncSession, email: str, password: str, name: Optional[str] = None
) -> AdminUser:
    admin = AdminUser(email=email.lower(), password_hash=hash_password(password), name=name)
    session.add(admin)
    await session.commit()
    await session.refresh(admin)
    return admin
