import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import GOOGLE_PLACES_API_KEY, logger
from database import get_session
from db.base import Location
from db.serializers import location_to_dict
from services.google_places import fetch_place_autocomplete, fetch_place_details

router = APIRouter(prefix="/locations", tags=["locations"])


async def _search_db_locations(session: AsyncSession, query: str, limit: int = 5) -> list[Location]:
    pattern = f"%{query}%"
    result = await session.execute(
        select(Location)
        .where(
            or_(
                Location.name.ilike(pattern),
                Location.city.ilike(pattern),
                Location.state.ilike(pattern),
                Location.pincode.ilike(pattern),
            )
        )
        .limit(limit)
    )
    return list(result.scalars().all())


@router.get("/search")
async def search_locations(
    query: str = Query(..., min_length=1, description="Search text (min 3 chars for results)"),
    session: AsyncSession = Depends(get_session),
):
    """Autosuggest locations: DB first (min 3 chars), then Google Places."""
    try:
        if len(query) < 3:
            return {"success": True, "locations": []}

        db_locations = await _search_db_locations(session, query, limit=5)
        results = [location_to_dict(loc) for loc in db_locations]
        seen_place_ids = {loc.google_place_id for loc in db_locations if loc.google_place_id}

        if len(results) >= 5 or not GOOGLE_PLACES_API_KEY:
            return {"success": True, "locations": results}

        predictions = fetch_place_autocomplete(query, limit=5 - len(results))
        for prediction in predictions:
            place_id = prediction.get("place_id")
            if not place_id or place_id in seen_place_ids:
                continue

            existing = await session.execute(
                select(Location).where(Location.google_place_id == place_id)
            )
            cached = existing.scalar_one_or_none()
            if cached:
                results.append(location_to_dict(cached))
                seen_place_ids.add(place_id)
                continue

            details = fetch_place_details(place_id)
            if not details:
                continue

            location = Location(
                name=details["name"] or prediction.get("description", ""),
                lat=details["lat"],
                lng=details["lng"],
                pincode=details["pincode"],
                city=details["city"],
                state=details["state"],
                google_place_id=place_id,
                source="google",
            )
            session.add(location)
            await session.commit()
            await session.refresh(location)
            results.append(location_to_dict(location))
            seen_place_ids.add(place_id)

            if len(results) >= 5:
                break

        return {"success": True, "locations": results}
    except Exception as e:
        logger.error(f"Error in search_locations: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
