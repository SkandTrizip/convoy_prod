from fastapi import APIRouter

from routers import (
    admin,
    auth,
    bookings,
    kyc,
    locations,
    misc,
    notifications,
    posts,
    search,
    trucks,
    users,
    vehicles,
    verification,
)

api_router = APIRouter(prefix="/api")

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(locations.router)
api_router.include_router(kyc.router)
api_router.include_router(vehicles.router)
api_router.include_router(verification.router)
api_router.include_router(posts.router)
api_router.include_router(search.router)
api_router.include_router(trucks.router)
api_router.include_router(bookings.router)
api_router.include_router(notifications.router)
api_router.include_router(admin.router)
api_router.include_router(misc.router)
