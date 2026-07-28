"""Admin JWT auth — mirrors middleware/auth.py's driver-JWT pattern, but on its
own secret (ADMIN_JWT_SECRET) and its own table (AdminUser), so an admin token
and a driver token are never interchangeable."""
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import ADMIN_JWT_EXPIRE_HOURS, ADMIN_JWT_SECRET, logger
from database import get_session
from db.base import AdminUser
from db.serializers import parse_uuid

ALGORITHM = "HS256"
admin_bearer_scheme = HTTPBearer(auto_error=False)


def create_admin_access_token(admin_id: str, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": admin_id,
        "email": email,
        "iat": now,
        "exp": now + timedelta(hours=ADMIN_JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, ADMIN_JWT_SECRET, algorithm=ALGORITHM)


def decode_admin_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, ADMIN_JWT_SECRET, algorithms=[ALGORITHM])


async def get_current_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(admin_bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> AdminUser:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = decode_admin_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="Token expired") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail="Invalid token") from e

    admin_id = payload.get("sub")
    if not admin_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    try:
        admin_uuid = parse_uuid(str(admin_id))
    except ValueError as e:
        raise HTTPException(status_code=401, detail="Invalid token subject") from e

    result = await session.execute(select(AdminUser).where(AdminUser.id == admin_uuid))
    admin = result.scalar_one_or_none()
    if not admin:
        raise HTTPException(status_code=401, detail="Admin not found")
    if not admin.is_active:
        raise HTTPException(status_code=403, detail="Admin account disabled")

    return admin
