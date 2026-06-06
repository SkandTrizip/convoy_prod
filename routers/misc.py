from fastapi import APIRouter

router = APIRouter(tags=["misc"])


@router.get("/truck-types")
async def get_truck_types():
    """Get list of available truck types"""
    truck_types = [
        "Open Body",
        "Container",
        "Trailer",
        "Flatbed",
        "Tanker",
        "Pickup",
        "Mini Truck",
        "LCV",
    ]
    return {"success": True, "truckTypes": truck_types}


@router.get("/")
async def root():
    return {"message": "Convoy API - Truck Discovery & Availability Marketplace"}
