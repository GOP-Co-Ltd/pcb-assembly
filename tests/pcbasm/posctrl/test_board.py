"""BoardTransformMeasurer の4点 affine 計測の仕様テスト."""

from collections.abc import Callable, Iterable

import numpy as np
import pytest
from pytest_mock import MockerFixture
from shapely import Polygon

from pcbasm import gcode
from pcbasm.config import CornerOffsets, ReferencePoint
from pcbasm.geometry import Compose, Matrix2d, Point2d, Shift
from pcbasm.pcb import Outline
from pcbasm.posctrl.board import BoardTransformMeasurer


def _sequence(
    points: Iterable[Point2d],
) -> tuple[Callable[[], Point2d], list[Point2d]]:
    """Point2dを順に返し、返した観測値を記録する callable を作る."""
    iterator = iter(points)
    observed: list[Point2d] = []

    def observe() -> Point2d:
        point = next(iterator)
        observed.append(point)
        return point

    return observe, observed


class TestBoardTransformMeasurer:
    """4コーナーから得る board→machine affine 変換のテスト."""

    @pytest.fixture
    def klipper(self, mocker: MockerFixture):
        return mocker.Mock()

    @pytest.fixture
    def stage(self, mocker: MockerFixture):
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        stage.move.return_value = gcode.GCode("G1")
        return stage

    def test_measure_recovers_known_affine_and_observes_corners_in_fixed_order(
        self, klipper, stage
    ):
        """既知affineを復元し、TL→TR→BL→BRの順に4点を観測する."""
        outline = Outline(
            Polygon([(0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0)])
        )
        reference_point = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(3.0, -4.0),
                bottom_left=(5.0, 5.0),
                bottom_right=(-5.0, 5.0),
            ),
        )
        move_targets = (
            Point2d(10.0, 20.0),
            Point2d(112.0, 18.0),
            Point2d(14.0, 77.0),
            Point2d(104.0, 77.0),
        )
        board_markers = (
            Point2d(1.0, -2.0),
            Point2d(103.0, -4.0),
            Point2d(5.0, 55.0),
            Point2d(95.0, 55.0),
        )
        expected = Compose(
            [
                Matrix2d(np.array([[1.2, 0.3], [-0.4, 0.8]])),
                Shift(7.0, -3.0),
            ]
        )
        adjust_reference, observed = _sequence(
            expected.apply(marker) for marker in board_markers
        )
        measurer = BoardTransformMeasurer(
            adjust_reference=adjust_reference,
            klipper=klipper,
            stage=stage,
            outline=outline,
            reference_point=reference_point,
        )

        actual = measurer.measure()

        assert isinstance(actual, Compose)
        assert len(actual) == 2
        assert isinstance(actual[0], Matrix2d)
        assert isinstance(actual[1], Shift)
        for point in (Point2d(0.0, 0.0), Point2d(20.0, 30.0), Point2d(-5.0, 80.0)):
            expected_point = expected.apply(point)
            actual_point = actual.apply(point)
            assert actual_point.x == pytest.approx(expected_point.x)
            assert actual_point.y == pytest.approx(expected_point.y)

        moves = [
            (call.kwargs["x"], call.kwargs["y"]) for call in stage.move.call_args_list
        ]
        assert moves == [(target.x, target.y) for target in move_targets]
        assert observed == [expected.apply(marker) for marker in board_markers]
        assert klipper.send_gcode.call_count == 4

    @pytest.mark.parametrize("perturbed_corner_index", range(4))
    def test_every_corner_contributes_to_affine_result(
        self, klipper, stage, perturbed_corner_index: int
    ):
        """どの1点だけに誤差があっても4点最小二乗結果へ等しく寄与する."""
        outline = Outline(Polygon([(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]))
        reference_point = ReferencePoint(
            x=0.0,
            y=0.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(0.0, 0.0),
                top_right=(0.0, 0.0),
                bottom_left=(0.0, 0.0),
                bottom_right=(0.0, 0.0),
            ),
        )
        measurements = [
            Point2d(0.0, 0.0),
            Point2d(2.0, 0.0),
            Point2d(0.0, 2.0),
            Point2d(2.0, 2.0),
        ]
        point = measurements[perturbed_corner_index]
        measurements[perturbed_corner_index] = Point2d(point.x + 4.0, point.y)
        adjust_reference, _ = _sequence(measurements)
        measurer = BoardTransformMeasurer(
            adjust_reference=adjust_reference,
            klipper=klipper,
            stage=stage,
            outline=outline,
            reference_point=reference_point,
        )

        actual = measurer.measure().apply(Point2d(1.0, 1.0))

        assert actual.x == pytest.approx(2.0)
        assert actual.y == pytest.approx(1.0)
