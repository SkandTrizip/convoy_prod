from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.serializers import admin_to_dict
from middleware.admin_auth import create_admin_access_token, get_current_admin
from models import AdminLoginRequest, CreateAdminUserRequest
from services.admin_users import create_admin_user, get_admin_by_email, verify_password

router = APIRouter(prefix="/admin-auth", tags=["admin-auth"])


@router.post("/login")
async def admin_login(
    request: AdminLoginRequest,
    session: AsyncSession = Depends(get_session),
):
    """Admin email+password login. Public — this is the entry point into the
    admin JWT namespace, everything else under /api/admin* requires the token
    this returns."""
    try:
        admin = await get_admin_by_email(session, request.email)
        if not admin or not admin.is_active or not verify_password(
            request.password, admin.password_hash
        ):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        access_token = create_admin_access_token(str(admin.id), admin.email)
        return {
            "success": True,
            "accessToken": access_token,
            "tokenType": "bearer",
            "admin": admin_to_dict(admin),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in admin_login: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admins", dependencies=[Depends(get_current_admin)])
async def create_admin(
    request: CreateAdminUserRequest,
    session: AsyncSession = Depends(get_session),
):
    """Create a new admin account. Requires an existing admin's token — this
    is how the team grows the admin list without ever touching the DB by hand."""
    try:
        existing = await get_admin_by_email(session, request.email)
        if existing:
            raise HTTPException(status_code=409, detail="An admin with this email already exists")

        admin = await create_admin_user(session, request.email, request.password, request.name)
        return {"success": True, "admin": admin_to_dict(admin)}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in create_admin: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
