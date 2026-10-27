from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import User
from db.serializers import one_to_one_call_to_dict, parse_uuid
from middleware.auth import get_current_user
from models import StartOneToOneCallRequest
from services import one_to_one_calls

router = APIRouter(prefix="/calls", tags=["calls"])


def _credentials_response(creds: dict) -> dict:
    return {
        "success": True,
        "callId": str(creds["callId"]),
        "appId": creds["appId"],
        "channelName": creds["channelName"],
        "uid": creds["uid"],
        "rtcToken": creds["rtcToken"],
        "rtmToken": creds["rtmToken"],
        "expiresAt": creds["expiresAt"].isoformat(),
    }


@router.post("", status_code=201)
async def start_call(
    request: StartOneToOneCallRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Starts a 1:1 call room as its host, returns Agora RTC/RTM credentials."""
    try:
        creds = await one_to_one_calls.start_call(session, current_user.id, request.title, request.hostName)
        return _credentials_response(creds)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in start_call: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def list_calls(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Currently-live 1:1 call rooms, newest first."""
    try:
        items, total = await one_to_one_calls.list_live(session, limit=limit, offset=offset)
        return {
            "success": True,
            "calls": [one_to_one_call_to_dict(c) for c in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.error(f"Error in list_calls: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{call_id}")
async def get_call(
    call_id: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(get_current_user),
):
    try:
        call = await one_to_one_calls.get_call(session, parse_uuid(call_id))
        return {"success": True, "call": one_to_one_call_to_dict(call)}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error in get_call: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{call_id}/join")
async def join_call(
    call_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Claims the single guest slot (or rejoins it after a dropped
    connection), returns Agora RTC/RTM credentials."""
    try:
        creds = await one_to_one_calls.join_call(session, parse_uuid(call_id), current_user.id)
        return _credentials_response(creds)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error in join_call: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{call_id}/end")
async def end_call(
    call_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    try:
        call = await one_to_one_calls.get_call(session, parse_uuid(call_id))
        if current_user.id not in (call.host_user_id, call.guest_user_id):
            raise HTTPException(status_code=403, detail="Not authorized for this call")
        call = await one_to_one_calls.end_call(session, call.id)
        return {"success": True, "call": one_to_one_call_to_dict(call)}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error in end_call: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
