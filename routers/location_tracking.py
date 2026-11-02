"""Location sharing for 48 hours after a truck post (see services/location_tracking.py).

The window is opened by the server when a post is created or reactivated; the app asks for its
state, reports a position now and then while it is on, and stops it on logout.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from db.base import User
from middleware.auth import get_current_user
from models import LocationReportRequest
from services import location_tracking as svc

router = APIRouter(prefix="/location-tracking", tags=["location-tracking"])


@router.get("/status")
async def tracking_status(
    session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)
):
    """Is the window on, and until when? `intervalMinutes` is how often the app should report."""
    return {"success": True, **await svc.get_status(session, current_user.id)}


@router.post("/location")
async def report_location(
    body: LocationReportRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """One position. Updates the user's last known location and the pin of their live post. Always
    `200`: `active: false` means the window is over (stop the background job), `recorded: false`
    that this one was too soon after the last to be stored."""
    place = body.model_dump(include={"name", "city", "state", "pincode"})
    state = await svc.record_location(session, current_user.id, body.lat, body.lng, body.accuracy, place)
    return {"success": True, **state}


@router.post("/stop")
async def stop_tracking(
    session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)
):
    """End the window now (the app calls this on logout) and forget the last position."""
    return {"success": True, **await svc.stop_tracking(session, current_user.id, svc.LOGOUT)}
