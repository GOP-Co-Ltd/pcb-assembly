"""CameraRotationMeasurer の仕様テスト.

計画書 memory/agents/implementation-planner/pad-alignment.md
「posctrl/offset.py（変更）」に基づく。

新契約: observe() -> Transform。内部で o_i = observe().apply(Point2d(0, 0)) と
縮約し、measure() は不変（(A2) アンカー: R = Rotation.from_points(Δs, o2 − o1)）。
klipper は FakeKlipper、stage はその上の実 XYZStage を使う。
"""

from collections.abc import Callable, Iterable

import pytest

from pcbasm.align.camera_rotation import CameraRotationMeasurer
from pcbasm.geometry import Point2d, Point3d, Rotation, Shift, Transform
from pcbasm.hal import XYZStage
from pcbasm.hal.fake import FakeKlipper


def _sequence(transforms: Iterable[Transform]) -> Callable[[], Transform]:
    """呼び出しごとに transforms を先頭から順に返す observe 関数を作る."""
    iterator = iter(transforms)
    return lambda: next(iterator)


class TestCameraRotationMeasurer:
    """CameraRotationMeasurer の2点法計測のテスト."""

    @pytest.fixture
    def klipper(self) -> FakeKlipper:
        klipper = FakeKlipper()
        klipper.set_status("gcode_move", "gcode_position", [5.0, 6.0, 7.0, 0.0])
        return klipper

    @pytest.fixture
    def stage(self, klipper: FakeKlipper) -> XYZStage:
        return XYZStage(klipper)

    @pytest.mark.parametrize(
        ("o1", "o2", "expected_degrees"),
        [
            (Shift(0.0, 0.0), Shift(0.0, 10.0), 90.0),
            (Shift(0.0, 0.0), Shift(7.0710678, 7.0710678), 45.0),
            # o1 が非ゼロでも差分 o2 − o1 = (0, 10) のみが効く
            (Shift(0.5, -0.3), Shift(0.5, 9.7), 90.0),
        ],
    )
    def test_measure_returns_rotation_from_points_of_offset_change(
        self,
        klipper,
        stage,
        o1: Transform,
        o2: Transform,
        expected_degrees: float,
    ):
        """Measure() = Rotation.from_points(移動ベクトル, o2 − o1)（(A2) ピン）."""
        measurer = CameraRotationMeasurer(
            observe=_sequence([o1, o2]),
            klipper=klipper,
            stage=stage,
            move_distance=10.0,
            settle_sec=0.0,
        )

        transform = measurer.measure()

        expected = Rotation(expected_degrees).apply(Point2d(1.0, 0.0))
        actual = transform.apply(Point2d(1.0, 0.0))
        assert actual.x == pytest.approx(expected.x, abs=1e-6)
        assert actual.y == pytest.approx(expected.y, abs=1e-6)

    def test_measure_moves_in_x_and_returns_to_start_position(self, klipper, stage):
        """X方向へ move_distance 相対移動し、計測後に元の位置へ戻る."""
        measurer = CameraRotationMeasurer(
            observe=_sequence([Shift(0.0, 0.0), Shift(0.0, 10.0)]),
            klipper=klipper,
            stage=stage,
            move_distance=10.0,
            settle_sec=0.0,
        )

        measurer.measure()

        assert klipper.g1_moves() == [
            {"x": 15.0, "y": 6.0, "f": 3000.0},
            {"x": 5.0, "y": 6.0, "z": 7.0, "f": 3000.0},
        ]
        assert len(klipper.sent) == 2
        assert stage.get_position() == Point3d(5.0, 6.0, 7.0)
