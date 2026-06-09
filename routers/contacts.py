from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import User
from db.serializers import parse_uuid
from middleware.auth import require_path_user
from models import MutualContactsRequest, SyncContactsRequest
from services.contacts import find_mutual_contacts, sync_user_contacts

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.post("/sync/{user_id}")
async def sync_contacts(
    user_id: str,
    body: SyncContactsRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Store hashed device contacts for mutual-connection matching."""
    try:
        result = await sync_user_contacts(
            session,
            parse_uuid(user_id),
            [c.model_dump() for c in body.contacts],
        )
        return {
            "success": True,
            "message": f"Synced {result['syncedCount']} contact(s)",
            **result,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("Error in sync_contacts: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/mutual/{user_id}")
async def get_mutual_contacts(
    user_id: str,
    body: MutualContactsRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_path_user),
):
    """Find phone-book contacts shared between the logged-in user and another user."""
    try:
        viewer_id = current_user.id
        other_id = parse_uuid(body.otherUserId)
        mutuals = await find_mutual_contacts(session, viewer_id, other_id)
        return {
            "success": True,
            "message": f"Found {len(mutuals)} mutual contact(s)",
            "mutualContacts": mutuals,
            "totalMutuals": len(mutuals),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("Error in get_mutual_contacts: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/mutual/{user_id}")
async def get_mutual_contacts_query(
    user_id: str,
    otherUserId: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_path_user),
):
    """GET variant for driver profile — compare with ?otherUserId=."""
    try:
        mutuals = await find_mutual_contacts(
            session,
            current_user.id,
            parse_uuid(otherUserId),
        )
        return {
            "success": True,
            "message": f"Found {len(mutuals)} mutual contact(s)",
            "mutualContacts": mutuals,
            "totalMutuals": len(mutuals),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("Error in get_mutual_contacts_query: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
