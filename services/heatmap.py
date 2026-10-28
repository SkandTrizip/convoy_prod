"""Heatmap data: live truck locations and reel posting locations, aggregated
into H3 hexagon cells.

Only per-cell counts ever leave this module — never a user id and never an
exact coordinate. The client picks the H3 resolution from its map zoom (coarse
cells zoomed out for a regional glow, fine cells zoomed in for detail).

Rows are first collapsed in SQL to distinct points rounded to POINT_PRECISION
decimals (~11 m) with a count, so the Python/H3 work scales with the number of
distinct points rather than the number of rows.

Cells at FINE_RESOLUTION or finer (~0.5 km and below) are only returned when
they hold at least FINE_RESOLUTION_MIN_ACTIVITY items, so a single truck or
reel can't be pinpointed from a cell centre.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Literal

import h3
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config import HEATMAP_REEL_WINDOW_DAYS
from db.base import TruckRoute, User
from db.serializers import profile_photo_url
from services.contacts import attach_mutuals_to_listings
from services.destinations import destination_to_dict, get_destinations_for_routes
from services.post_expiry import ACTIVE_POST_STATUSES

HeatmapKind = Literal["trucks", "reels"]

MIN_RESOLUTION = 3
MAX_RESOLUTION = 9
DEFAULT_RESOLUTION = 6

FINE_RESOLUTION = 8
FINE_RESOLUTION_MIN_ACTIVITY = 3

# Safety ceiling on cells per response. At the finest resolution each returned
# cell holds >= FINE_RESOLUTION_MIN_ACTIVITY items, so this is only reached at
# very large scale; the response says so via `truncated`.
MAX_CELLS = 5000

POINT_PRECISION = 4

# Real-pin mode (trucks, zoomed in on a city). A viewport is a small box, so the
# box a client may ask for is capped; the pin count is capped too, nearest the
# centre first, and the response says so via `truncated`.
DEFAULT_PINS = 500
MAX_PINS = 1000
MAX_PIN_SPAN_DEG = 3.0

# A truck post's location is where the truck is now (`current_location`, a
# JSONB {lat, lng, ...} the app always sends on create/edit); the post's
# origin point is the fallback for any row where that JSON is missing or
# malformed, so one bad row can never fail the whole query. Coordinates are
# range-checked on the outer query for the same reason. "Live" matches
# services/post_expiry: an active status AND not past expires_at, which stays
# correct even if the background sweep hasn't flipped the status yet.
#
# The one definition of "where is each live truck", shared by the heat cells and
# the real pins so the two can never disagree about which trucks exist.
LIVE_TRUCK_POSITIONS = """
    SELECT id, lat, lng FROM (
        SELECT
            id,
            CASE WHEN has_current THEN (current_location->>'lat')::numeric
                 ELSE ST_Y(origin_location::geometry)::numeric END AS lat,
            CASE WHEN has_current THEN (current_location->>'lng')::numeric
                 ELSE ST_X(origin_location::geometry)::numeric END AS lng
        FROM (
            SELECT
                id,
                current_location,
                origin_location,
                (jsonb_typeof(current_location->'lat') = 'number'
                 AND jsonb_typeof(current_location->'lng') = 'number') AS has_current
            FROM truck_routes
            WHERE status = ANY(:statuses) AND expires_at > :now
        ) live
    ) pts
    WHERE lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180
"""

TRUCK_POINTS_SQL = text(
    f"""
    SELECT round(lat, :precision) AS lat, round(lng, :precision) AS lng, COUNT(*) AS n
    FROM ({LIVE_TRUCK_POSITIONS}) positions
    GROUP BY 1, 2
    """
)

# One extra row beyond :limit tells us whether the result was cut off.
TRUCK_PINS_SQL = text(
    f"""
    SELECT p.id, p.lat::float8 AS lat, p.lng::float8 AS lng, tr.truck_type, tr.capacity
    FROM ({LIVE_TRUCK_POSITIONS}) p
    JOIN truck_routes tr ON tr.id = p.id
    WHERE p.lat::float8 BETWEEN CAST(:min_lat AS double precision) AND CAST(:max_lat AS double precision)
      AND p.lng::float8 BETWEEN CAST(:min_lng AS double precision) AND CAST(:max_lng AS double precision)
    ORDER BY power(p.lat::float8 - CAST(:center_lat AS double precision), 2)
           + power(p.lng::float8 - CAST(:center_lng AS double precision), 2),
             p.id
    LIMIT :limit_plus_one
    """
)

REEL_POINTS_SQL = text(
    """
    SELECT
        round(ST_Y(location::geometry)::numeric, :precision) AS lat,
        round(ST_X(location::geometry)::numeric, :precision) AS lng,
        COUNT(*) AS n
    FROM videos
    WHERE status = 'ready' AND location IS NOT NULL AND created_at >= :since
    GROUP BY 1, 2
    """
)


def min_cell_activity(resolution: int) -> int:
    return FINE_RESOLUTION_MIN_ACTIVITY if resolution >= FINE_RESOLUTION else 1


def aggregate_points(
    points: Iterable[tuple[float, float, int]], resolution: int
) -> tuple[list[dict[str, Any]], bool]:
    """Bucket (lat, lng, count) points into H3 cells of `resolution`.

    Returns (cells, truncated), cells sorted by count descending. Pure — no I/O —
    so the bucketing/threshold/cap rules are unit-testable on their own.
    """
    counts: dict[str, int] = {}
    for lat, lng, n in points:
        cell = h3.latlng_to_cell(lat, lng, resolution)
        counts[cell] = counts.get(cell, 0) + n

    threshold = min_cell_activity(resolution)
    cells = []
    for cell, n in counts.items():
        if n < threshold:
            continue
        center_lat, center_lng = h3.cell_to_latlng(cell)
        cells.append({"h3Cell": cell, "centerLat": center_lat, "centerLng": center_lng, "count": n})

    # Ties broken by cell id so the response is deterministic across requests.
    cells.sort(key=lambda c: (-c["count"], c["h3Cell"]))
    truncated = len(cells) > MAX_CELLS
    return cells[:MAX_CELLS], truncated


async def build_heatmap(session: AsyncSession, kind: HeatmapKind, resolution: int) -> dict[str, Any]:
    now = datetime.utcnow()
    if kind == "trucks":
        result = await session.execute(
            TRUCK_POINTS_SQL,
            {"statuses": list(ACTIVE_POST_STATUSES), "now": now, "precision": POINT_PRECISION},
        )
    else:
        result = await session.execute(
            REEL_POINTS_SQL,
            {
                "since": now - timedelta(days=HEATMAP_REEL_WINDOW_DAYS),
                "precision": POINT_PRECISION,
            },
        )

    points = [(float(row.lat), float(row.lng), int(row.n)) for row in result]
    cells, truncated = aggregate_points(points, resolution)
    return {
        "success": True,
        "type": kind,
        "resolution": resolution,
        "minCellActivity": min_cell_activity(resolution),
        "cellCount": len(cells),
        "truncated": truncated,
        "generatedAt": now.isoformat(),
        "cells": cells,
    }


def validate_bbox(min_lat: float, min_lng: float, max_lat: float, max_lng: float) -> None:
    """Raises ValueError with a client-safe message for a box we won't serve."""
    if min_lat >= max_lat or min_lng >= max_lng:
        raise ValueError("minLat/minLng must be less than maxLat/maxLng")
    if max_lat - min_lat > MAX_PIN_SPAN_DEG or max_lng - min_lng > MAX_PIN_SPAN_DEG:
        raise ValueError(
            f"The requested area is too large (at most {MAX_PIN_SPAN_DEG:g} degrees per side); zoom in further"
        )


async def build_truck_pins(
    session: AsyncSession,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    limit: int = DEFAULT_PINS,
) -> dict[str, Any]:
    """Live trucks inside a map box, as light pins (no contact details — those are
    fetched per truck by get_live_post_detail when a pin is tapped)."""
    validate_bbox(min_lat, min_lng, max_lat, max_lng)
    limit = max(1, min(limit, MAX_PINS))
    now = datetime.utcnow()
    result = await session.execute(
        TRUCK_PINS_SQL,
        {
            "statuses": list(ACTIVE_POST_STATUSES),
            "now": now,
            "min_lat": min_lat,
            "max_lat": max_lat,
            "min_lng": min_lng,
            "max_lng": max_lng,
            "center_lat": (min_lat + max_lat) / 2,
            "center_lng": (min_lng + max_lng) / 2,
            "limit_plus_one": limit + 1,
        },
    )
    rows = result.all()
    truncated = len(rows) > limit
    pins = [
        {
            "id": str(row.id),
            "lat": row.lat,
            "lng": row.lng,
            "truckType": row.truck_type,
            "capacity": row.capacity,
        }
        for row in rows[:limit]
    ]
    return {
        "success": True,
        "count": len(pins),
        "truncated": truncated,
        "generatedAt": now.isoformat(),
        "pins": pins,
    }


def _has_coordinates(location: Any) -> bool:
    return (
        isinstance(location, dict)
        and isinstance(location.get("lat"), (int, float))
        and isinstance(location.get("lng"), (int, float))
    )


async def get_live_post_detail(
    session: AsyncSession, post_id: uuid.UUID, viewer_id: uuid.UUID
) -> dict[str, Any] | None:
    """Full details of one live post, in the same shape as an item of
    POST /api/search/trucks (so the app parses it with the same model), or None if
    the post doesn't exist or is no longer live (expired since the pin was drawn)."""
    now = datetime.utcnow()
    route = (
        await session.execute(
            select(TruckRoute).where(
                TruckRoute.id == post_id,
                TruckRoute.status.in_(ACTIVE_POST_STATUSES),
                TruckRoute.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    if route is None:
        return None

    destinations = (await get_destinations_for_routes(session, [route.id])).get(str(route.id), [])
    route_id = str(route.id)
    item: dict[str, Any] = {
        "truck_id": str(route.truck_id),
        "truck_route_id": route_id,
        "truck_number": route.truck_number,
        "truck_type": route.truck_type,
        "capacity": route.capacity,
        "length": route.length_ft,
        "height": route.height_ft,
        "contactName": route.contact_name,
        "contactNumber": route.contact_number,
        "origin": route.origin_name,
        "status": route.status,
        "_id": route_id,
        "userId": str(route.user_id),
        "vehicleId": str(route.truck_id),
        "vehicleNumber": route.truck_number,
        "truckType": route.truck_type,
        "originLocation": route.origin,
        # Same fallback as the pin position: a missing/malformed current location
        # falls back to the origin, so the app's parser always gets coordinates.
        "currentLocation": route.current_location if _has_coordinates(route.current_location) else route.origin,
        "createdAt": route.created_at,
        "expiresAt": route.expires_at,
        "destinations": [destination_to_dict(d) for d in destinations],
    }

    user = (await session.execute(select(User).where(User.id == route.user_id))).scalar_one_or_none()
    if user:
        item["userName"] = user.name or "Unknown"
        item["userPhoto"] = profile_photo_url(user)
        item["userMobile"] = user.mobile

    [item] = await attach_mutuals_to_listings(session, viewer_id, [item])
    return item
