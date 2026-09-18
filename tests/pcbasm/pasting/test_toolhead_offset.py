"""多点ツールヘッドオフセット計測の仕様テスト.

``ToolheadOffsetProcedure`` は自前 HAL の fake（``FakeKlipper`` / ``FakeCamera``）に
実 ``XYZStage`` / ``ProbeExecutor`` / ``PasteApplicator`` を組み合わせ、送信 G-code で検証する。
"""

import math
from datetime import UTC, datetime

import cv2
import numpy as np
import pytest
from shapely import Point as ShapelyPoint, Polygon

from pcbasm.config import Machine
from pcbasm.geometry import Identity, Point2d, Shift
from pcbasm.hal import XYZStage
from pcbasm.pasting.toolhead_offset import (
    MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
    ToolheadOffsetDiagnostics,
    ToolheadOffsetFailure,
    ToolheadOffsetProcedure,
    ToolheadOffsetResult,
    ToolheadOffsetSample,
    plan_toolhead_offset_points,
)
from pcbasm.pcb import PcbFile
from pcbasm.posctrl import BoardCalibrationResult
from pcbasm.vision import Image
from pcbasm.vision.calibration import CalibrationResult
from tests.helpers import TESTING_CONFIG_DIR, TESTING_DATA_DIR, FakeCamera, FakeKlipper

CALIBRATED_AT = datetime(2026, 7, 17, 12, 0, 0)
LED_BLINKER = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
PPM = 10.0
FOCUS_Z = 12.0
BOARD_SHIFT = Point2d(100.0, 50.0)


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
    result = ToolheadOffsetResult.measure(
        samples,
        tolerance=0.05,
        point_spacing=5.0,
        edge_margin=5.0,
        calibrated_at=CALIBRATED_AT,
    )
    assert result is not None
    return result


class TestToolheadOffsetSample:
    def test_from_positions_calculates_dispense_minus_camera(self):
        sample = ToolheadOffsetSample.from_positions(
            board_position=Point2d(x=15.0, y=20.0),
            dispense_position=Point2d(x=101.5, y=197.7),
            camera_position=Point2d(x=100.0, y=200.0),
        )

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
    def test_measure_returns_none_for_too_few_samples(self, sample_count: int):
        result = ToolheadOffsetResult.measure(
            [_sample(index, Point2d(x=1.0, y=-2.0)) for index in range(sample_count)],
            tolerance=0.05,
            point_spacing=5.0,
            edge_margin=5.0,
            calibrated_at=CALIBRATED_AT,
        )

        assert result is None

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

        assert stable is not None
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


class TestPlanToolheadOffsetPoints:
    def test_points_are_sampled_from_top_left_in_row_major_order(self):
        outline = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)])

        points, error = plan_toolhead_offset_points(
            outline,
            point_count=9,
            point_spacing=5.0,
            edge_margin=5.0,
            paste_diameter_max=2.0,
        )

        assert error is None
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

        points, _ = plan_toolhead_offset_points(
            outline,
            point_count=9,
            point_spacing=5.0,
            edge_margin=5.0,
            paste_diameter_max=2.0,
        )

        assert points is not None
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

        points, _ = plan_toolhead_offset_points(
            outline,
            point_count=6,
            point_spacing=5.0,
            edge_margin=0.0,
            paste_diameter_max=2.0,
        )

        assert points is not None
        assert len(points) == 6
        assert Point2d(x=13.5, y=6.0) in points

    def test_points_stay_inside_concave_board_safe_area(self):
        outline = Polygon([(0, 0), (50, 0), (50, 20), (20, 20), (20, 50), (0, 50)])
        safe_area = outline.buffer(-(5.0 + 2.0 / 2))

        points, _ = plan_toolhead_offset_points(
            outline,
            point_count=9,
            point_spacing=5.0,
            edge_margin=5.0,
            paste_diameter_max=2.0,
        )

        assert points is not None
        assert len(points) == 9
        assert all(safe_area.covers(ShapelyPoint(point.x, point.y)) for point in points)

    def test_rejects_board_that_cannot_fit_requested_points(self):
        outline = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])

        points, error = plan_toolhead_offset_points(
            outline,
            point_count=9,
            point_spacing=5.0,
            edge_margin=5.0,
            paste_diameter_max=2.0,
        )

        assert points is None
        assert error is not None
        assert "9" in error
        assert "配置可能" in error

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

        points, error = plan_toolhead_offset_points(
            Polygon([(0, 0), (30, 0), (30, 30), (0, 30)]),
            point_count=int(params["point_count"]),
            point_spacing=float(params["point_spacing"]),
            edge_margin=float(params["edge_margin"]),
            paste_diameter_max=float(params["paste_diameter_max"]),
        )

        assert points is None
        assert error is not None
        assert expected in error

    def test_rejects_empty_outline(self):
        points, error = plan_toolhead_offset_points(
            Polygon(),
            point_count=9,
            point_spacing=5.0,
            edge_margin=5.0,
            paste_diameter_max=2.0,
        )

        assert points is None
        assert error is not None
        assert "基板外形" in error


def _failure(index: int, image: Image | None) -> ToolheadOffsetFailure:
    return ToolheadOffsetFailure(
        index=index,
        board_position=Point2d(x=6.0, y=11.0),
        reason="円検出に3回失敗しました",
        image=image,
    )


class TestToolheadOffsetDiagnostics:
    def test_from_outcomes_counts_samples_and_pins_minimum(self):
        image = Image(np.zeros((50, 50, 3), dtype=np.uint8))
        failures = [_failure(3, image), _failure(7, None)]

        diagnostics = ToolheadOffsetDiagnostics.from_outcomes(
            10, failures, [_sample(i, Point2d(0.0, 0.0)) for i in range(8)]
        )

        assert diagnostics == ToolheadOffsetDiagnostics(
            requested_point_count=10,
            minimum_valid_point_count=MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
            failures=tuple(failures),
            successful_point_count=8,
        )

    def test_to_dict_matches_diagnostics_json_shape(self):
        image = Image(np.zeros((50, 50, 3), dtype=np.uint8))
        diagnostics = ToolheadOffsetDiagnostics(
            requested_point_count=10,
            minimum_valid_point_count=MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
            failures=(_failure(3, image), _failure(7, None)),
            successful_point_count=8,
        )

        data = diagnostics.to_dict()

        assert data == {
            "requested_point_count": 10,
            "minimum_valid_point_count": 5,
            "successful_point_count": 8,
            "failures": [
                {
                    "index": 3,
                    "board_position": {"x": 6.0, "y": 11.0},
                    "reason": "円検出に3回失敗しました",
                    "image": "toolhead_offset_failure_03.png",
                },
                {
                    "index": 7,
                    "board_position": {"x": 6.0, "y": 11.0},
                    "reason": "円検出に3回失敗しました",
                    "image": None,
                },
            ],
        }


def _board_result(klipper: FakeKlipper, camera: FakeCamera) -> BoardCalibrationResult:
    return BoardCalibrationResult(
        machine=Machine(TESTING_CONFIG_DIR / "machine.toml"),
        klipper=klipper,
        stage=XYZStage(klipper.readonly),
        camera=camera,
        calibration=CalibrationResult(
            pixel_per_mm=PPM,
            square_size_mm=1.0,
            mean_distance_px=PPM,
            std_distance_px=0.0,
            resolution=(640, 480),
            crop_size=(400, 400),
            calibrated_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC),
            z_position=FOCUS_Z,
        ),
        offset_transform=Identity(),
        board_transform=Shift(BOARD_SHIFT.x, BOARD_SHIFT.y),
        pcb=PcbFile(LED_BLINKER),
    )


class TestToolheadOffsetProcedure:
    """Probe → deposit → measure の 1 点分の機械手順."""

    @pytest.fixture
    def klipper(self) -> FakeKlipper:
        return FakeKlipper()

    @pytest.fixture
    def result(self, klipper: FakeKlipper) -> BoardCalibrationResult:
        blank = Image(np.zeros((480, 640, 3), dtype=np.uint8))
        return _board_result(klipper, FakeCamera([blank]))

    @pytest.fixture
    def result_with_dot(self, klipper: FakeKlipper) -> BoardCalibrationResult:
        """ROI 中心に塗布痕があるカメラ."""
        frame = np.full((480, 640, 3), 200, dtype=np.uint8)
        cv2.circle(frame, (320, 240), 5, (60, 60, 60), -1)
        return _board_result(klipper, FakeCamera([Image(frame)]))

    @pytest.fixture
    def procedure(self, result: BoardCalibrationResult) -> ToolheadOffsetProcedure:
        return ToolheadOffsetProcedure(
            result,
            tolerance=0.1,
            lift_height=5.0,
            diameter_min=0.4,
            diameter_max=2.0,
            point_spacing=5.0,
        )

    def test_probe_moves_nozzle_to_dispense_position_and_reads_surface_z(
        self,
        procedure: ToolheadOffsetProcedure,
        result: BoardCalibrationResult,
        klipper: FakeKlipper,
    ):
        klipper.set_status("probe", "last_z_result", 1.5)
        toolhead = result.machine.paste_dispenser.toolhead
        board = Point2d(x=10.0, y=20.0)

        probed = procedure.probe(board)

        assert probed.point.board == board
        assert probed.point.camera == Point2d(
            x=board.x + BOARD_SHIFT.x, y=board.y + BOARD_SHIFT.y
        )
        assert probed.point.dispense.x == pytest.approx(
            probed.point.camera.x + toolhead.x
        )
        assert probed.point.dispense.y == pytest.approx(
            probed.point.camera.y + toolhead.y
        )
        assert probed.surface_z == 1.5
        lines = klipper.sent_lines
        first_move = klipper.g1_moves()[0]
        assert first_move["x"] == pytest.approx(probed.point.dispense.x)
        assert first_move["y"] == pytest.approx(probed.point.dispense.y)
        assert "z" not in first_move
        assert lines.index("PROBE") > lines.index(
            next(line for line in lines if line.startswith("G1"))
        )

    def test_deposit_dispenses_at_surface_z_plus_paste_height(
        self,
        procedure: ToolheadOffsetProcedure,
        result: BoardCalibrationResult,
        klipper: FakeKlipper,
    ):
        klipper.set_status("probe", "last_z_result", 1.5)
        probed = procedure.probe(Point2d(x=10.0, y=20.0))

        with procedure.applicator() as applicator:
            klipper.clear_sent()
            procedure.deposit(applicator, probed, amount_ul=0.1)
            moves = klipper.g1_moves()
            paste_height = applicator.default_params.paste_height_mm

        assert result.machine.paste_dispenser.paste_height == "auto"
        approach, descend = moves[0], moves[1]
        assert approach["x"] == pytest.approx(probed.point.dispense.x)
        assert approach["y"] == pytest.approx(probed.point.dispense.y)
        assert approach["z"] == pytest.approx(1.5 + paste_height + 5.0)
        assert descend["z"] == pytest.approx(1.5 + paste_height)

    def test_measure_returns_sample_when_paste_dot_is_at_roi_center(
        self,
        klipper: FakeKlipper,
        result_with_dot: BoardCalibrationResult,
    ):
        """ROI 中心の塗布痕からオフセットを求める."""
        procedure = ToolheadOffsetProcedure(
            result_with_dot,
            tolerance=0.1,
            lift_height=5.0,
            diameter_min=0.5,
            diameter_max=2.0,
            point_spacing=5.0,
        )
        probed = procedure.probe(Point2d(x=10.0, y=20.0))
        toolhead = result_with_dot.machine.paste_dispenser.toolhead
        # 円検出はステージがカメラ位置に居る状態で行われる
        klipper.set_status(
            "gcode_move",
            "gcode_position",
            [probed.point.camera.x, probed.point.camera.y, FOCUS_Z, 0.0],
        )

        outcome = procedure.measure(1, probed)

        assert isinstance(outcome, ToolheadOffsetSample)
        assert outcome.camera_position.x == pytest.approx(
            probed.point.camera.x, abs=0.1
        )
        assert outcome.camera_position.y == pytest.approx(
            probed.point.camera.y, abs=0.1
        )
        assert outcome.offset.x == pytest.approx(toolhead.x, abs=0.1)
        assert outcome.offset.y == pytest.approx(toolhead.y, abs=0.1)

    def test_measure_returns_failure_with_roi_image_when_no_circle(
        self,
        procedure: ToolheadOffsetProcedure,
        klipper: FakeKlipper,
    ):
        probed = procedure.probe(Point2d(x=10.0, y=20.0))
        klipper.clear_sent()

        outcome = procedure.measure(3, probed)

        assert isinstance(outcome, ToolheadOffsetFailure)
        assert outcome.index == 3
        assert outcome.board_position == probed.point.board
        assert "円検出" in outcome.reason
        assert outcome.image is not None
        assert outcome.image.size == procedure.roi_size
        assert outcome.image_filename == "toolhead_offset_failure_03.png"
        move = klipper.g1_moves()[0]
        assert move["x"] == pytest.approx(probed.point.camera.x)
        assert move["y"] == pytest.approx(probed.point.camera.y)
        assert move["z"] == pytest.approx(FOCUS_Z)
