from fastapi import APIRouter, Depends

from middleware.auth import get_current_user
from routers import (
    activity,
    admin,
    auth,
    bookings,
    contacts,
    kyc,
    locations,
    misc,
    notifications,
    posts,
    search,
    users,
    vehicles,
    verification,
)

api_router = APIRouter(prefix="/api")

# Public routes (no JWT)
api_router.include_router(auth.router)
api_router.include_router(misc.router)

# Protected routes (Bearer JWT required)
protected_router = APIRouter(dependencies=[Depends(get_current_user)])
protected_router.include_router(users.router)
protected_router.include_router(contacts.router)
protected_router.include_router(locations.router)
protected_router.include_router(kyc.router)
protected_router.include_router(vehicles.router)
protected_router.include_router(verification.router)
protected_router.include_router(posts.router)
protected_router.include_router(search.router)
protected_router.include_router(bookings.router)
protected_router.include_router(notifications.router)
protected_router.include_router(admin.router)
protected_router.include_router(activity.router)
api_router.include_router(protected_router)
