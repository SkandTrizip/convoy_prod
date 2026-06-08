from datetime import date
from typing import Any

from geoalchemy2 import WKTElement
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import User


def make_geography_point(lng: float, lat: float) -> WKTElement:
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


async def search_truck_routes_spatial(
    session: AsyncSession,
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    radius_km: float = 150,
    available_date: date | None = None,
    truck_type: str | None = None,
    include_user_info: bool = False,
) -> list[dict[str, Any]]:
    """PostGIS search using ST_DWithin on origin and destination geography columns."""
    radius_m = radius_km * 1000
    search_date = available_date or date.today()

    origin_point = f"SRID=4326;POINT({origin_lng} {origin_lat})"
    dest_point = f"SRID=4326;POINT({destination_lng} {destination_lat})"

    truck_type_filter = "AND tr.truck_type = :truck_type" if truck_type else ""
    params: dict[str, Any] = {
        "origin_point": origin_point,
        "dest_point": dest_point,
        "radius_m": radius_m,
        "search_date": search_date,
    }
    if truck_type:
        params["truck_type"] = truck_type

    query = text(f"""
        SELECT
            tr.id,
            tr.truck_id,
            tr.user_id,
            tr.truck_number,
            tr.truck_type,
            tr.capacity,
            tr.origin_name,
            tr.destination_name,
            tr.origin,
            tr.destination,
            tr.current_location,
            tr.available_date,
            tr.status,
            tr.created_at,
            tr.expires_at,
            ST_Distance(tr.origin_location, ST_GeogFromText(:origin_point)) / 1000.0 AS origin_distance_km,
            ST_Distance(tr.destination_location, ST_GeogFromText(:dest_point)) / 1000.0 AS destination_distance_km
        FROM truck_routes tr
        WHERE tr.status IN ('available', 'active')
          AND tr.available_date >= :search_date
          AND tr.expires_at > NOW()
          AND ST_DWithin(tr.origin_location, ST_GeogFromText(:origin_point), :radius_m)
          AND ST_DWithin(tr.destination_location, ST_GeogFromText(:dest_point), :radius_m)
          {truck_type_filter}
        ORDER BY
            ST_Distance(tr.origin_location, ST_GeogFromText(:origin_point))
            + ST_Distance(tr.destination_location, ST_GeogFromText(:dest_point))
        LIMIT 1000
    """)

    result = await session.execute(query, params)
    rows = result.mappings().all()

    matches: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {
            "truck_id": str(row["truck_id"]),
            "truck_route_id": str(row["id"]),
            "truck_number": row["truck_number"],
            "truck_type": row["truck_type"],
            "capacity": row["capacity"],
            "origin": row["origin_name"],
            "destination": row["destination_name"],
            "origin_distance_km": round(float(row["origin_distance_km"]), 2),
            "destination_distance_km": round(float(row["destination_distance_km"]), 2),
            "available_date": row["available_date"].isoformat(),
            "status": row["status"],
            "_id": str(row["id"]),
            "userId": str(row["user_id"]),
            "vehicleId": str(row["truck_id"]),
            "vehicleNumber": row["truck_number"],
            "truckType": row["truck_type"],
            "originLocation": row["origin"],
            "destinationLocation": row["destination"],
            "currentLocation": row["current_location"],
            "createdAt": row["created_at"],
            "expiresAt": row["expires_at"],
        }

        if include_user_info:
            user_result = await session.execute(
                select(User).where(User.id == row["user_id"])
            )
            user = user_result.scalar_one_or_none()
            if user:
                item["userName"] = user.name or "Unknown"
                item["userPhoto"] = user.profile_photo
                item["userMobile"] = user.mobile

        matches.append(item)

    return matches
