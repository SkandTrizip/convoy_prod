from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import JWT_EXPIRE_HOURS, JWT_SECRET, logger
from database import get_session
from db.base import User
from db.serializers import parse_uuid
from notifications.repositories import device_repository

ALGORITHM = "HS256"
bearer_scheme = HTTPBearer(auto_error=False)


def create_access_token(user_id: str, mobile: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "mobile": mobile,
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
) -> User:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="Token expired") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail="Invalid token") from e

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    try:
        user_uuid = parse_uuid(str(user_id))
    except ValueError as e:
        raise HTTPException(status_code=401, detail="Invalid token subject") from e

    result = await session.execute(select(User).where(User.id == user_uuid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if user.account_status == "suspended":
        raise HTTPException(status_code=403, detail="Account suspended")

    # Opportunistic device-liveness signal — no dedicated "heartbeat" endpoint;
    # any authenticated request that names its device counts as evidence the
    # device is still in active use. Silently a no-op for requests without the
    # header (old app versions, admin calls) or an unrecognized device_id.
    await device_repository.touch_last_seen(session, x_device_id)

    return user


def authorize_user_id(user_id: str, current_user: User) -> None:
    if str(current_user.id) != str(parse_uuid(user_id)):
        raise HTTPException(status_code=403, detail="Not authorized for this user")


async def require_path_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
) -> User:
    authorize_user_id(user_id, current_user)
    return current_user
