"""多点ツールヘッドオフセット計測の仕様テスト."""

import math
from datetime import datetime

import pytest
from shapely import Point as ShapelyPoint, Polygon

from pcbasm.geometry import Point2d
from pcbasm.pasting import (
    MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
    ToolheadOffsetResult,
    ToolheadOffsetSample,
    plan_toolhead_offset_points,
)

CALIBRATED_AT = datetime(2026, 7, 17, 12, 0, 0)


def _sample(index: int, offset: Point2d) -> ToolheadOffsetSample:
    board_position = Point2d(x=float(index * 5), y=10.0)
    camera_position = Point2d(x=100.0 + index, y=200.0 + index)
    return ToolheadOffsetSample(
        board_position=board_position,
        dispense_position=camera_position + offset,
        camera_position=camera_position,
        offset=offset,
    )


def _make_result() -> ToolheadOffsetResult:
    samples = (
        _sample(0, Point2d(x=1.0, y=-2.0)),
        _sample(1, Point2d(x=2.0, y=-4.0)),
        _sample(2, Point2d(x=3.0, y=-6.0)),
        _sample(3, Point2d(x=4.0, y=-8.0)),
        _sample(4, Point2d(x=5.0, y=-10.0)),
    )
    return ToolheadOffsetResult.measure(
        samples,
        tolerance=0.05,
        point_spacing=5.0,
        edge_margin=5.0,
        calibrated_at=CALIBRATED_AT,
    )


class TestToolheadOffsetSample:
    def test_from_positions_calculates_dispense_minus_camera(self):
        sample = ToolheadOffsetSample.from_positions(
            board_position=Point2d(x=15.0, y=20.0),
            dispense_position=Point2d(x=101.5, y=197.7),
            camera_position=Point2d(x=100.0, y=200.0),
        )

        assert sample.board_position == Point2d(x=15.0, y=20.0)
        assert sample.dispense_position == Point2d(x=101.5, y=197.7)
        assert sample.camera_position == Point2d(x=100.0, y=200.0)
        assert sample.offset.x == pytest.approx(1.5)
        assert sample.offset.y == pytest.approx(-2.3)


class TestToolheadOffsetResult:
    def test_measure_uses_component_mean_and_population_standard_deviation(self):
        result = _make_result()

        assert result.offset.x == pytest.approx(3.0)
        assert result.offset.y == pytest.approx(-6.0)
        assert result.standard_deviation.x == pytest.approx(math.sqrt(2))
        assert result.standard_deviation.y == pytest.approx(math.sqrt(8))
        assert len(result.samples) == 5
        assert result.tolerance == pytest.approx(0.05)
        assert result.point_spacing == pytest.approx(5.0)
        assert result.edge_margin == pytest.approx(5.0)
        assert result.calibrated_at == CALIBRATED_AT

    @pytest.mark.parametrize(
        "sample_count", range(MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT)
    )
    def test_measure_rejects_too_few_samples(self, sample_count: int):
        with pytest.raises(ValueError) as exc_info:
            ToolheadOffsetResult.measure(
                [
                    _sample(index, Point2d(x=1.0, y=-2.0))
                    for index in range(sample_count)
                ],
                tolerance=0.05,
                point_spacing=5.0,
                edge_margin=5.0,
                calibrated_at=CALIBRATED_AT,
            )

        assert "最低 5 点" in str(exc_info.value)

    def test_is_within_tolerance_uses_each_axis_standard_deviation(self):
        stable = ToolheadOffsetResult.measure(
            [
                _sample(index, Point2d(x=1.0 + index * 0.01, y=-2.0))
                for index in range(MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT)
            ],
            tolerance=0.05,
            point_spacing=5.0,
            edge_margin=5.0,
            calibrated_at=CALIBRATED_AT,
        )
        variable = _make_result()

        assert stable.is_within_tolerance is True
        assert variable.is_within_tolerance is False

    def test_to_dict_from_dict_roundtrip_uses_multipoint_format(self):
        result = _make_result()

        data = result.to_dict()
        restored = ToolheadOffsetResult.from_dict(data)

        assert restored == result
        assert set(data) == {
            "offset",
            "standard_deviation",
            "samples",
            "tolerance",
            "point_spacing",
            "edge_margin",
            "calibrated_at",
        }
        assert data["offset"] == {"x": 3.0, "y": -6.0}
        assert data["standard_deviation"]["x"] == pytest.approx(math.sqrt(2))
        assert len(data["samples"]) == 5
        assert data["samples"][0]["board_position"] == {"x": 0.0, "y": 10.0}
        assert data["point_spacing"] == pytest.approx(5.0)
        assert data["edge_margin"] == pytest.approx(5.0)

    def test_save_load_roundtrip(self, tmp_path):
        result = _make_result()
        path = tmp_path / "toolhead_offset.json"

        result.save(path)

        assert ToolheadOffsetResult.load(path) == result


class TestPlanToolheadOffsetPoints:
    def test_points_are_sampled_from_top_left_in_row_major_order(self):
        outline = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)])

        points = plan_toolhead_offset_points(
            outline,
            point_count=9,
            point_spacing=5.0,
            edge_margin=5.0,
            paste_diameter_max=2.0,
        )

        assert points == (
            Point2d(x=6.0, y=6.0),
            Point2d(x=11.0, y=6.0),
            Point2d(x=16.0, y=6.0),
            Point2d(x=21.0, y=6.0),
            Point2d(x=6.0, y=11.0),
            Point2d(x=11.0, y=11.0),
            Point2d(x=16.0, y=11.0),
            Point2d(x=21.0, y=11.0),
            Point2d(x=6.0, y=16.0),
        )

    def test_points_keep_paste_edge_clear_of_outline_and_cutout(self):
        outline = Polygon(
            [(0, 0), (40, 0), (40, 40), (0, 40)],
            holes=[[(18, 18), (22, 18), (22, 22), (18, 22)]],
        )
        clearance = 5.0 + 2.0 / 2
        safe_area = outline.buffer(-clearance)

        points = plan_toolhead_offset_points(
            outline,
            point_count=9,
            point_spacing=5.0,
            edge_margin=5.0,
            paste_diameter_max=2.0,
        )

        assert len(points) == 9
        assert all(safe_area.covers(ShapelyPoint(point.x, point.y)) for point in points)
        assert all(
            (left - right).norm >= 5.0 - 1e-9
            for index, left in enumerate(points)
            for right in points[index + 1 :]
        )

    def test_non_grid_candidate_avoids_error_around_cutout(self):
        outline = Polygon(
            [(0, 0), (15, 0), (15, 15), (0, 15)],
            holes=[[(3, 3), (11, 3), (11, 11), (3, 11)]],
        )

        points = plan_toolhead_offset_points(
            outline,
            point_count=6,
            point_spacing=5.0,
            edge_margin=0.0,
            paste_diameter_max=2.0,
        )

        assert len(points) == 6
        assert Point2d(x=13.5, y=6.0) in points

    def test_points_stay_inside_concave_board_safe_area(self):
        outline = Polygon([(0, 0), (50, 0), (50, 20), (20, 20), (20, 50), (0, 50)])
        safe_area = outline.buffer(-(5.0 + 2.0 / 2))

        points = plan_toolhead_offset_points(
            outline,
            point_count=9,
            point_spacing=5.0,
            edge_margin=5.0,
            paste_diameter_max=2.0,
        )

        assert len(points) == 9
        assert all(safe_area.covers(ShapelyPoint(point.x, point.y)) for point in points)

    def test_rejects_board_that_cannot_fit_requested_points(self):
        outline = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])

        with pytest.raises(ValueError) as exc_info:
            plan_toolhead_offset_points(
                outline,
                point_count=9,
                point_spacing=5.0,
                edge_margin=5.0,
                paste_diameter_max=2.0,
            )

        message = str(exc_info.value)
        assert "9" in message
        assert "配置可能" in message

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"point_count": 0}, "point_count"),
            ({"point_spacing": 0.0}, "point_spacing"),
            ({"edge_margin": -0.1}, "edge_margin"),
            ({"paste_diameter_max": -0.1}, "paste_diameter_max"),
        ],
    )
    def test_rejects_invalid_planning_parameters(
        self, overrides: dict[str, int | float], expected: str
    ):
        params: dict[str, int | float] = {
            "point_count": 9,
            "point_spacing": 5.0,
            "edge_margin": 5.0,
            "paste_diameter_max": 2.0,
        }
        params.update(overrides)

        with pytest.raises(ValueError) as exc_info:
            plan_toolhead_offset_points(
                Polygon([(0, 0), (30, 0), (30, 30), (0, 30)]),
                point_count=int(params["point_count"]),
                point_spacing=float(params["point_spacing"]),
                edge_margin=float(params["edge_margin"]),
                paste_diameter_max=float(params["paste_diameter_max"]),
            )

        assert expected in str(exc_info.value)

    def test_rejects_empty_outline(self):
        with pytest.raises(ValueError) as exc_info:
            plan_toolhead_offset_points(
                Polygon(),
                point_count=9,
                point_spacing=5.0,
                edge_margin=5.0,
                paste_diameter_max=2.0,
            )

        assert "基板外形" in str(exc_info.value)
