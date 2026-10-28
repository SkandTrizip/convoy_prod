"""Reels of one heatmap area: the reels that made a heat cell glow.

The Reels heatmap aggregates reels into H3 cells (services/heatmap.py). Tapping a
hotspot opens the feed of exactly that cell's reels, so what the user sees agrees
with what the heat counted. This module holds the pure parts (validating a cell,
its geometry, exact membership, the area label); the query lives in
services/videos.py::list_area_feed.

Membership is exact H3, applied after a cheap radius prefilter in SQL: each point is
rounded to the heatmap's POINT_PRECISION first, the same way the heat counts it, so
the two cannot disagree about which cell a reel is in.

Cells finer than MAX_AREA_RESOLUTION are refused: the public heatmap hides tiny cells
(they could pinpoint one reel), and an area feed for a ~170 m cell would let a
hand-made request do the same.
"""

import math
from collections import Counter
from collections.abc import Iterable
from typing import Any

import h3

from services.heatmap import POINT_PRECISION

MIN_AREA_RESOLUTION = 3
MAX_AREA_RESOLUTION = 7

# Most candidates examined per request, best-ranked first. Generous for a city-sized
# cell; only a very coarse, very busy cell can reach it (reported as `capped`).
MAX_CANDIDATES = 2000

# Slack added to the prefilter radius so a reel near a cell's edge is never dropped
# before the exact H3 check.
_RADIUS_MARGIN_M = 100.0


def parse_area_cell(value: str) -> tuple[str, int]:
    """(cell, resolution) for a valid area cell, else ValueError with a client-safe
    message."""
    cell = (value or "").strip()
    try:
        valid = h3.is_valid_cell(cell)
    except Exception:  # h3 raises on some malformed input instead of returning False
        valid = False
    if not valid:
        raise ValueError("h3Cell is not a valid H3 cell")
    resolution = h3.get_resolution(cell)
    if not MIN_AREA_RESOLUTION <= resolution <= MAX_AREA_RESOLUTION:
        raise ValueError(
            f"h3Cell must be at resolution {MIN_AREA_RESOLUTION} to {MAX_AREA_RESOLUTION}"
        )
    return cell, resolution


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def cell_geometry(cell: str) -> tuple[float, float, float]:
    """(centre_lat, centre_lng, radius_m): a circle around the cell centre that
    contains the whole cell (its farthest corner plus a margin)."""
    center_lat, center_lng = h3.cell_to_latlng(cell)
    farthest = max(
        _haversine_m(center_lat, center_lng, lat, lng) for lat, lng in h3.cell_to_boundary(cell)
    )
    return center_lat, center_lng, farthest + _RADIUS_MARGIN_M


def in_cell(lat: float, lng: float, cell: str, resolution: int) -> bool:
    """Whether a reel at (lat, lng) is in `cell` — rounded like the heatmap rounds."""
    return (
        h3.latlng_to_cell(round(lat, POINT_PRECISION), round(lng, POINT_PRECISION), resolution)
        == cell
    )


def area_label(places: Iterable[tuple[str | None, str | None]]) -> str | None:
    """The most common "City, State" among the reels' own location labels (or just
    one of them if the other is missing); None if none of them has a label."""
    counts: Counter[str] = Counter()
    for city, state in places:
        parts = [p.strip() for p in (city, state) if p and p.strip()]
        if parts:
            counts[", ".join(parts)] += 1
    if not counts:
        return None
    # most_common is stable for ties in first-seen order, which is rank order here.
    return counts.most_common(1)[0][0]


def area_info(
    cell: str, resolution: int, label: str | None, total: int, capped: bool
) -> dict[str, Any]:
    return {
        "h3Cell": cell,
        "resolution": resolution,
        "label": label,
        "total": total,
        "capped": capped,
    }
