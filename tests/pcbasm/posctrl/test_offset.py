"""OffsetTransformMeasurer の仕様テスト.

計画書 memory/agents/implementation-planner/pad-alignment.md
「posctrl/offset.py（変更）」に基づく。

新契約: observe() -> Transform。内部で o_i = observe().apply(Point2d(0, 0)) と
縮約し、measure() は不変（(A2) アンカー: R = Rotation.from_points(Δs, o2 − o1)）。
klipper / stage は自前 HAL のため mocker.Mock を使用する。
"""

from collections.abc import Callable, Iterable

import pytest
from pytest_mock import MockerFixture

from pcbasm import gcode
from pcbasm.geometry import Point2d, Point3d, Rotation, Shift, Transform
from pcbasm.posctrl.offset import OffsetTransformMeasurer


def _sequence(transforms: Iterable[Transform]) -> Callable[[], Transform]:
    """呼び出しごとに transforms を先頭から順に返す observe 関数を作る."""
    iterator = iter(transforms)
    return lambda: next(iterator)


class TestOffsetTransformMeasurer:
    """OffsetTransformMeasurer の2点法計測のテスト."""

    @pytest.fixture
    def klipper(self, mocker: MockerFixture):
        return mocker.Mock()

    @pytest.fixture
    def stage(self, mocker: MockerFixture):
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        stage.get_position.return_value = Point3d(5.0, 6.0, 7.0)
        stage.move.return_value = gcode.GCode("G1")
        return stage

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
        measurer = OffsetTransformMeasurer(
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
        measurer = OffsetTransformMeasurer(
            observe=_sequence([Shift(0.0, 0.0), Shift(0.0, 10.0)]),
            klipper=klipper,
            stage=stage,
            move_distance=10.0,
            settle_sec=0.0,
        )

        measurer.measure()

        assert stage.move.call_count == 2
        out_kwargs = stage.move.call_args_list[0].kwargs
        assert out_kwargs["x"] == pytest.approx(10.0)
        assert out_kwargs["y"] == pytest.approx(0.0)
        assert out_kwargs["relative"] is True
        back_kwargs = stage.move.call_args_list[1].kwargs
        assert back_kwargs["x"] == pytest.approx(5.0)
        assert back_kwargs["y"] == pytest.approx(6.0)
        assert back_kwargs["z"] == pytest.approx(7.0)
        assert klipper.send_gcode.call_count == 2
