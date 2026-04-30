"""build_paste_fill_path のテスト."""

import pytest
from shapely import Polygon

from pcb_assembly.control.pasting.fill_path import build_paste_fill_path
from pcb_assembly.geometry import Point2d


class TestBuildPasteFillPath:
    """build_paste_fill_path 関数のテスト."""

    def test_normal_polygon_picks_spiral(self):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])

        result = build_paste_fill_path(polygon, nozzle_diameter=1.0)

        assert len(result) > 2

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (0.3, 2.0, 0.34),
            (0.8, 5.0, 1.0),
        ],
    )
    def test_narrow_polygon_picks_linear(self, width, length, nozzle_diameter):
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])

        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        assert len(result) == 2
        expected_distance = max(width, length) - nozzle_diameter
        assert (result[1] - result[0]).norm == pytest.approx(
            expected_distance, abs=0.01
        )
        short_axis_value = min(width, length) / 2
        if width < length:
            assert result[0].x == pytest.approx(short_axis_value, abs=0.01)
            assert result[1].x == pytest.approx(short_axis_value, abs=0.01)
        else:
            assert result[0].y == pytest.approx(short_axis_value, abs=0.01)
            assert result[1].y == pytest.approx(short_axis_value, abs=0.01)

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (0.1, 0.1, 1.0),
            (1.0, 0.5, 1.0),
        ],
    )
    def test_too_small_polygon_returns_empty(self, width, length, nozzle_diameter):
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])

        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        assert result == []

    @pytest.mark.parametrize("nozzle_diameter", [0.0, -1.0, -0.001])
    def test_invalid_nozzle_diameter_raises_value_error(self, nozzle_diameter):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])

        with pytest.raises(ValueError, match="nozzle_diameter"):
            build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

    def test_empty_polygon_returns_empty(self):
        assert build_paste_fill_path(Polygon(), nozzle_diameter=1.0) == []

    def test_all_points_are_point2d(self):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])

        result = build_paste_fill_path(polygon, nozzle_diameter=1.0)

        assert all(isinstance(p, Point2d) for p in result)
