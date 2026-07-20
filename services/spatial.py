from typing import Any

from geoalchemy2 import WKTElement
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import User
from db.serializers import profile_photo_url


SEARCH_PAGE_SIZE = 10
# Safety ceiling on how many distance-ranked matches we'll ever consider paginating over.
SEARCH_MAX_MATCHES = 1000


def make_geography_point(lng: float, lat: float) -> WKTElement:
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


async def search_truck_routes_spatial(
    session: AsyncSession,
    origin_lat: float | None = None,
    origin_lng: float | None = None,
    destination_lat: float | None = None,
    destination_lng: float | None = None,
    radius_km: float = 150,
    truck_type: str | None = None,
    min_capacity: float | None = None,
    sort_by: str | None = None,
    page: int = 1,
) -> tuple[list[dict[str, Any]], int]:
    """PostGIS search: at least one of origin/destination is required. When both are
    given, origin must be within radius AND at least one of the route's (up to 5)
    destinations must be within radius of the searched destination (current behavior).
    When only one side is given, matching and ranking are based on that side alone,
    with no filtering on the other — e.g. an origin-only search returns posts with any
    destination, ranked by origin distance alone.

    sort_by can request "nearest_origin" or "nearest_destination" to rank by just that
    side instead of the combined default. If the requested side wasn't actually part of
    the search (e.g. "nearest_origin" with no origin given), it's silently ignored in
    favor of the default ranking below — never raises for this.

    De-dupes to one row per route via DISTINCT ON. When destination is part of the
    search, the nearest matching destination is picked and returned as "matchedDestination"
    (with its distance) alongside the full destination list; when destination isn't part
    of the search, matchedDestination/destination_distance_km are omitted entirely — there's
    nothing to have "matched" against. Same idea for origin_distance_km when origin isn't
    part of the search. Results are paginated (SEARCH_PAGE_SIZE per page); returns
    (page_of_matches, total_count) where total_count is capped at SEARCH_MAX_MATCHES, same
    safety ceiling the query has always applied.
    """
    # Local import: services/destinations.py imports make_geography_point from this
    # module, so importing it back at module level here would create a cycle.
    from services.destinations import destination_to_dict, get_destinations_for_routes

    has_origin = origin_lat is not None and origin_lng is not None
    has_destination = destination_lat is not None and destination_lng is not None
    if not has_origin and not has_destination:
        raise ValueError("At least one of origin or destination is required")

    radius_m = radius_km * 1000

    origin_point = f"SRID=4326;POINT({origin_lng} {origin_lat})" if has_origin else None
    dest_point = (
        f"SRID=4326;POINT({destination_lng} {destination_lat})" if has_destination else None
    )

    # CAST(... AS text) gives asyncpg an explicit type for the bound param — without it, a
    # parameter that's sometimes NULL (origin-only/destination-only searches) and only
    # ever used inside a function call can fail with "could not determine data type". Note:
    # `:param::text` (no CAST) trips up SQLAlchemy's text() bind-parameter parser instead.
    origin_filter = (
        "AND ST_DWithin(tr.origin_location, ST_GeogFromText(CAST(:origin_point AS text)), :radius_m)"
        if has_origin
        else ""
    )
    dest_filter = (
        "AND ST_DWithin(d.destination_location, ST_GeogFromText(CAST(:dest_point AS text)), :radius_m)"
        if has_destination
        else ""
    )
    truck_type_filter = "AND tr.truck_type = :truck_type" if truck_type else ""
    # NULL capacity trucks are excluded whenever a minimum is requested — `NULL >= x` is
    # never true in SQL, which is the right call: an unspecified capacity can't be
    # confirmed to meet the requirement.
    capacity_filter = "AND tr.capacity >= :min_capacity" if min_capacity is not None else ""

    if sort_by == "nearest_origin" and has_origin:
        rank_expr = "origin_distance_km"
    elif sort_by == "nearest_destination" and has_destination:
        rank_expr = "destination_distance_km"
    elif has_origin and has_destination:
        rank_expr = "origin_distance_km + destination_distance_km"
    elif has_origin:
        rank_expr = "origin_distance_km"
    else:
        rank_expr = "destination_distance_km"

    params: dict[str, Any] = {
        "origin_point": origin_point,
        "dest_point": dest_point,
        "radius_m": radius_m,
        "max_matches": SEARCH_MAX_MATCHES,
        "offset": (page - 1) * SEARCH_PAGE_SIZE,
        "page_size": SEARCH_PAGE_SIZE,
    }
    if truck_type:
        params["truck_type"] = truck_type
    if min_capacity is not None:
        params["min_capacity"] = min_capacity

    # Distance-ranked, deduped (one row per route), capped at SEARCH_MAX_MATCHES —
    # shared by both the count query and the page query below so they agree on the
    # same result set. The JOIN + DISTINCT ON is used even for origin-only searches
    # (where destination isn't filtered/ranked at all) purely to dedupe a route's up to
    # 5 destination rows back down to one row per route; `d.position` is a secondary
    # tiebreaker so that case deterministically keeps destination #1 rather than an
    # arbitrary one.
    ranked_matches = f"""
        SELECT * FROM (
            SELECT DISTINCT ON (tr.id)
                tr.id,
                tr.truck_id,
                tr.user_id,
                tr.truck_number,
                tr.truck_type,
                tr.capacity,
                tr.contact_name,
                tr.contact_number,
                tr.origin_name,
                tr.origin,
                tr.current_location,
                tr.status,
                tr.created_at,
                tr.expires_at,
                ST_Distance(tr.origin_location, ST_GeogFromText(CAST(:origin_point AS text))) / 1000.0 AS origin_distance_km,
                d.id AS matched_destination_id,
                d.destination AS matched_destination,
                ST_Distance(d.destination_location, ST_GeogFromText(CAST(:dest_point AS text))) / 1000.0 AS destination_distance_km
            FROM truck_routes tr
            JOIN truck_route_destinations d ON d.truck_route_id = tr.id
            WHERE tr.status IN ('available', 'active')
              AND tr.expires_at > NOW()
              {origin_filter}
              {dest_filter}
              {truck_type_filter}
              {capacity_filter}
            ORDER BY tr.id, ST_Distance(d.destination_location, ST_GeogFromText(CAST(:dest_point AS text))), d.position
        ) matched
        ORDER BY {rank_expr}
        LIMIT :max_matches
    """

    count_query = text(f"SELECT COUNT(*) FROM ({ranked_matches}) capped")
    page_query = text(f"""
        SELECT * FROM ({ranked_matches}) capped
        ORDER BY {rank_expr}
        OFFSET :offset LIMIT :page_size
    """)

    total_count = (await session.execute(count_query, params)).scalar_one()

    result = await session.execute(page_query, params)
    rows = result.mappings().all()

    destinations_by_route = await get_destinations_for_routes(
        session, [row["id"] for row in rows]
    )

    matches: list[dict[str, Any]] = []
    for row in rows:
        route_id = str(row["id"])
        item: dict[str, Any] = {
            "truck_id": str(row["truck_id"]),
            "truck_route_id": route_id,
            "truck_number": row["truck_number"],
            "truck_type": row["truck_type"],
            "capacity": row["capacity"],
            "contactName": row["contact_name"],
            "contactNumber": row["contact_number"],
            "origin": row["origin_name"],
            "status": row["status"],
            "_id": route_id,
            "userId": str(row["user_id"]),
            "vehicleId": str(row["truck_id"]),
            "vehicleNumber": row["truck_number"],
            "truckType": row["truck_type"],
            "originLocation": row["origin"],
            "currentLocation": row["current_location"],
            "createdAt": row["created_at"],
            "expiresAt": row["expires_at"],
            "destinations": [
                destination_to_dict(d) for d in destinations_by_route.get(route_id, [])
            ],
        }

        if has_origin:
            item["origin_distance_km"] = round(float(row["origin_distance_km"]), 2)
        if has_destination:
            item["destination_distance_km"] = round(float(row["destination_distance_km"]), 2)
            item["matchedDestination"] = {
                **row["matched_destination"],
                "_id": str(row["matched_destination_id"]),
                "distanceKm": round(float(row["destination_distance_km"]), 2),
            }

        user_result = await session.execute(select(User).where(User.id == row["user_id"]))
        user = user_result.scalar_one_or_none()
        if user:
            item["userName"] = user.name or "Unknown"
            item["userPhoto"] = profile_photo_url(user)
            item["userMobile"] = user.mobile

        matches.append(item)

    return matches, total_count
