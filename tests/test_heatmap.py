"""Unit tests for services/heatmap.py's pure aggregation rules (no DB, no network).

Run from the repo root: .venv/bin/python -m pytest tests/test_heatmap.py
"""

import h3
import pytest

from services import heatmap

DELHI = (28.6139, 77.2090)
MUMBAI = (19.0760, 72.8777)


def test_points_in_same_cell_are_summed():
    cells, truncated = heatmap.aggregate_points(
        [(*DELHI, 2), (DELHI[0] + 0.001, DELHI[1] + 0.001, 3)], resolution=6
    )
    assert truncated is False
    assert len(cells) == 1
    assert cells[0]["count"] == 5


def test_far_apart_points_get_separate_cells_sorted_by_count():
    cells, _ = heatmap.aggregate_points([(*MUMBAI, 1), (*DELHI, 4)], resolution=5)
    assert [c["count"] for c in cells] == [4, 1]


def test_cell_center_belongs_to_its_own_cell():
    cells, _ = heatmap.aggregate_points([(*DELHI, 1)], resolution=7)
    cell = cells[0]
    assert h3.latlng_to_cell(cell["centerLat"], cell["centerLng"], 7) == cell["h3Cell"]


def test_response_cells_expose_only_cell_center_and_count():
    cells, _ = heatmap.aggregate_points([(*DELHI, 1)], resolution=6)
    assert set(cells[0]) == {"h3Cell", "centerLat", "centerLng", "count"}


@pytest.mark.parametrize("resolution", [3, 4, 5, 6, 7])
def test_coarse_resolutions_return_a_lone_item(resolution):
    cells, _ = heatmap.aggregate_points([(*DELHI, 1)], resolution=resolution)
    assert len(cells) == 1


@pytest.mark.parametrize("resolution", [8, 9])
def test_fine_resolutions_hide_cells_below_the_privacy_threshold(resolution):
    assert heatmap.min_cell_activity(resolution) == 3
    assert heatmap.aggregate_points([(*DELHI, 1)], resolution)[0] == []
    assert heatmap.aggregate_points([(*DELHI, 2)], resolution)[0] == []
    shown, _ = heatmap.aggregate_points([(*DELHI, 3)], resolution)
    assert [c["count"] for c in shown] == [3]


def test_threshold_counts_the_cell_total_not_each_point():
    # Two distinct ~11m-rounded points that share one res-8 cell: 1 + 2 = 3 passes.
    cells, _ = heatmap.aggregate_points([(*DELHI, 1), (DELHI[0] + 0.0002, DELHI[1], 2)], resolution=8)
    assert [c["count"] for c in cells] == [3]


def test_empty_input_gives_no_cells():
    assert heatmap.aggregate_points([], resolution=6) == ([], False)


def test_results_are_capped_and_flagged_truncated(monkeypatch):
    monkeypatch.setattr(heatmap, "MAX_CELLS", 2)
    points = [(10.0, 70.0, 1), (15.0, 75.0, 5), (20.0, 80.0, 3), (25.0, 85.0, 2)]
    cells, truncated = heatmap.aggregate_points(points, resolution=4)
    assert truncated is True
    assert [c["count"] for c in cells] == [5, 3]


def test_ordering_is_deterministic_for_equal_counts():
    points = [(10.0, 70.0, 2), (15.0, 75.0, 2), (20.0, 80.0, 2)]
    first, _ = heatmap.aggregate_points(points, resolution=4)
    second, _ = heatmap.aggregate_points(list(reversed(points)), resolution=4)
    assert [c["h3Cell"] for c in first] == [c["h3Cell"] for c in second]


def test_resolution_bounds_match_the_documented_range():
    assert (heatmap.MIN_RESOLUTION, heatmap.MAX_RESOLUTION) == (3, 9)
    assert heatmap.MIN_RESOLUTION <= heatmap.DEFAULT_RESOLUTION <= heatmap.MAX_RESOLUTION


# --- real pins: box validation ---------------------------------------------------


def test_bbox_accepts_a_city_sized_box():
    heatmap.validate_bbox(28.4, 77.0, 28.9, 77.4)  # does not raise


@pytest.mark.parametrize(
    "box",
    [
        (28.9, 77.0, 28.4, 77.4),  # min lat above max lat
        (28.4, 77.4, 28.9, 77.0),  # min lng above max lng
        (28.4, 77.0, 28.4, 77.4),  # zero height
        (28.4, 77.0, 28.9, 77.0),  # zero width
    ],
)
def test_bbox_rejects_inverted_or_empty_boxes(box):
    with pytest.raises(ValueError):
        heatmap.validate_bbox(*box)


def test_bbox_rejects_an_area_too_large_to_be_a_viewport():
    with pytest.raises(ValueError, match="too large"):
        heatmap.validate_bbox(8.0, 68.0, 8.0 + heatmap.MAX_PIN_SPAN_DEG + 0.1, 70.0)
    with pytest.raises(ValueError, match="too large"):
        heatmap.validate_bbox(8.0, 68.0, 9.0, 68.0 + heatmap.MAX_PIN_SPAN_DEG + 0.1)


def test_pin_limits_are_sane():
    assert 1 <= heatmap.DEFAULT_PINS <= heatmap.MAX_PINS
