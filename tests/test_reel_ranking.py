"""Unit tests for services/reel_ranking.py and services/reel_area.py (no DB, no network).

Run from the repo root: .venv/bin/python -m pytest tests/test_reel_ranking.py
"""

from datetime import datetime

import h3
import pytest
from sqlalchemy import select

from db.base import Video

from services import reel_area, reel_ranking
from services.reel_ranking import popularity_score_py as score

DELHI = (28.6139, 77.2090)


# --- popularity (the trending score) ---------------------------------------------


def test_an_untouched_reel_still_ranks_by_recency():
    # With zero engagement every reel would tie at 0 without the "1 +"; newer must win.
    assert score(0, 0, 0, 0, age_hours=1) > score(0, 0, 0, 0, age_hours=48)


def test_more_engagement_scores_higher_at_the_same_age():
    assert score(10, 0, 0, 0, 24) > score(1, 0, 0, 0, 24)
    assert score(0, 5, 0, 0, 24) > score(0, 1, 0, 0, 24)
    assert score(0, 0, 5, 0, 24) > score(0, 0, 1, 0, 24)
    assert score(0, 0, 0, 500, 24) > score(0, 0, 0, 10, 24)


def test_a_share_is_worth_more_than_a_comment_than_a_like_than_a_view():
    assert score(0, 0, 1, 0, 24) > score(0, 1, 0, 0, 24) > score(1, 0, 0, 0, 24) > score(0, 0, 0, 1, 24)


def test_popularity_fades_with_age():
    assert score(50, 10, 5, 800, 2) > score(50, 10, 5, 800, 24) > score(50, 10, 5, 800, 24 * 20)


def test_a_good_new_reel_can_beat_an_old_big_one_but_not_forever():
    new_good = score(40, 8, 4, 500, age_hours=6)
    old_big = score(400, 80, 40, 5000, age_hours=24 * 25)
    assert new_good > old_big
    # ...while a huge reel a day old still beats a lukewarm one posted just now.
    assert score(2000, 400, 200, 30000, 24) > score(3, 0, 0, 20, 1)


def test_negative_age_is_treated_as_zero():
    assert score(5, 0, 0, 0, -3) == score(5, 0, 0, 0, 0)


def _order_by_sql(sort: str) -> str:
    """The ORDER BY part of the SQL that `sort` produces."""
    sql = str(select(Video.id).order_by(*reel_ranking.order_by(sort, datetime(2026, 1, 1))))
    return sql.split("ORDER BY", 1)[1].strip()


def test_latest_ordering_is_newest_first_with_a_stable_tie_break():
    assert _order_by_sql("latest") == "videos.created_at DESC, videos.id"


def test_popular_ordering_leads_with_the_score_and_keeps_the_tie_break():
    sql = _order_by_sql("popular")
    assert sql.endswith("DESC, videos.created_at DESC, videos.id")
    # The score is built from the four engagement counters and the reel's age.
    for column in ("like_count", "comment_count", "share_count", "view_count", "created_at"):
        assert column in sql


# --- area: cell validation -------------------------------------------------------


def test_a_real_coarse_cell_parses_to_its_resolution():
    cell = h3.latlng_to_cell(*DELHI, 6)
    assert reel_area.parse_area_cell(cell) == (cell, 6)
    assert reel_area.parse_area_cell(f"  {cell}  ")[0] == cell


@pytest.mark.parametrize("bad", ["", "   ", "nope", "123", "zzzzzzzzzzzzzzz", "8f28308280f18c2"[:10]])
def test_garbage_is_rejected(bad):
    with pytest.raises(ValueError, match="valid H3 cell"):
        reel_area.parse_area_cell(bad)


@pytest.mark.parametrize("res", [0, 1, 2, 8, 9, 12])
def test_resolutions_outside_the_public_range_are_rejected(res):
    with pytest.raises(ValueError, match="resolution"):
        reel_area.parse_area_cell(h3.latlng_to_cell(*DELHI, res))


@pytest.mark.parametrize("res", [3, 4, 5, 6, 7])
def test_every_public_resolution_is_accepted(res):
    assert reel_area.parse_area_cell(h3.latlng_to_cell(*DELHI, res))[1] == res


# --- area: geometry and exact membership -----------------------------------------


@pytest.mark.parametrize("res", [3, 5, 7])
def test_the_prefilter_circle_contains_the_whole_cell(res):
    cell = h3.latlng_to_cell(*DELHI, res)
    lat, lng, radius = reel_area.cell_geometry(cell)
    for vlat, vlng in h3.cell_to_boundary(cell):
        assert reel_area._haversine_m(lat, lng, vlat, vlng) < radius


def test_the_circle_is_only_modestly_bigger_than_the_cell():
    cell = h3.latlng_to_cell(*DELHI, 7)
    _, _, radius = reel_area.cell_geometry(cell)
    # Res 7 cells have ~1.2 km edges; the prefilter radius must stay in that league.
    assert 1000 < radius < 2000


def test_a_point_in_the_cell_matches_and_a_neighbour_does_not():
    cell = h3.latlng_to_cell(*DELHI, 7)
    lat, lng = h3.cell_to_latlng(cell)
    assert reel_area.in_cell(lat, lng, cell, 7)
    neighbour = next(n for n in h3.grid_disk(cell, 1) if n != cell)
    nlat, nlng = h3.cell_to_latlng(neighbour)
    assert not reel_area.in_cell(nlat, nlng, cell, 7)


def test_membership_rounds_points_exactly_like_the_heatmap_does():
    # A point whose rounded and unrounded positions fall in different cells must be
    # judged by the ROUNDED one, because that is what the heatmap counted.
    cell = h3.latlng_to_cell(*DELHI, 7)
    for n in h3.grid_disk(cell, 1):
        if n == cell:
            continue
        lat, lng = h3.cell_to_latlng(n)
        rounded_cell = h3.latlng_to_cell(round(lat, 4), round(lng, 4), 7)
        assert reel_area.in_cell(lat, lng, rounded_cell, 7)


# --- area: label ----------------------------------------------------------------


def test_label_is_the_most_common_place():
    places = [("Delhi", "Delhi"), ("Noida", "Uttar Pradesh"), ("Delhi", "Delhi"), (None, None)]
    assert reel_area.area_label(places) == "Delhi, Delhi"


def test_label_copes_with_a_missing_half_and_with_nothing_at_all():
    assert reel_area.area_label([("Pune", None), ("Pune", None)]) == "Pune"
    assert reel_area.area_label([(None, "Goa")]) == "Goa"
    assert reel_area.area_label([(None, None), ("", "  ")]) is None
    assert reel_area.area_label([]) is None


def test_label_ties_go_to_the_better_ranked_reel():
    assert reel_area.area_label([("A", "X"), ("B", "Y")]) == "A, X"


def test_area_info_shape():
    assert reel_area.area_info("c", 6, "Delhi", 3, False) == {
        "h3Cell": "c", "resolution": 6, "label": "Delhi", "total": 3, "capped": False,
    }
