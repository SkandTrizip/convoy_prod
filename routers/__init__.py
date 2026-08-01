from fastapi import APIRouter, Depends

from middleware.auth import get_current_user
from routers import (
    activity,
    admin,
    admin_auth,
    auth,
    bookings,
    campaigns,
    contacts,
    kyc,
    locations,
    misc,
    notifications,
    posts,
    scratch_cards,
    search,
    users,
    vehicles,
    verification,
    wallet,
)

api_router = APIRouter(prefix="/api")

# Public routes (no JWT)
api_router.include_router(auth.router)
api_router.include_router(misc.router)

# Admin routes — own JWT namespace (see middleware/admin_auth.py), independent
# of the driver JWT below. admin_auth.router's /login is public; everything
# else under it and all of admin.router/campaigns.router requires an admin token.
api_router.include_router(admin_auth.router)
api_router.include_router(admin.router)
api_router.include_router(campaigns.router)

# Protected routes (driver Bearer JWT required)
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
protected_router.include_router(wallet.router)
protected_router.include_router(scratch_cards.router)
protected_router.include_router(activity.router)
api_router.include_router(protected_router)
