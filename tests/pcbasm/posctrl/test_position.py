"""XYPositionAdjustor の仕様テスト.

計画書 memory/agents/implementation-planner/pad-alignment.md
「posctrl/position.py（変更 — 破壊変更）」に基づく。

新契約: observe() -> Transform（カメラ mm 空間・想定→観測）。ループ内で
offset = offset_transform.apply(observe().apply(Point2d(0, 0))) として変位へ
縮約し、補正式 target = pos − offset（(A4) アンカー）は従来と不変。
klipper / stage は自前 HAL のため mocker.Mock を使用する。
"""

from collections.abc import Callable, Iterable

import pytest
from pytest_mock import MockerFixture

from pcbasm import gcode
from pcbasm.geometry import (
    Compose,
    Identity,
    Point2d,
    Point3d,
    Rotation,
    Shift,
    Transform,
)
from pcbasm.posctrl.position import XYPositionAdjustor


def _sequence(transforms: Iterable[Transform]) -> Callable[[], Transform]:
    """呼び出しごとに transforms を先頭から順に返す observe 関数を作る."""
    iterator = iter(transforms)
    return lambda: next(iterator)


class TestXYPositionAdjustor:
    """XYPositionAdjustor の収束・符号・例外のテスト."""

    @pytest.fixture
    def klipper(self, mocker: MockerFixture):
        return mocker.Mock()

    @pytest.fixture
    def stage(self, mocker: MockerFixture):
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        stage.get_position.return_value = Point3d(10.0, 20.0, 5.0)
        stage.move.return_value = gcode.GCode("G1")
        return stage

    def test_adjust_converges_with_shrinking_shifts_and_returns_final_target(
        self, klipper, stage
    ):
        """縮小する Shift 列で収束し、最終目標 pos − offset を返す.

        1回目 (0.4,−0.2) は tolerance=0.1 超 → (9.6, 20.2) へ補正移動。 2回目
        (0.02,0.01) で収束 → 戻り値 (9.98, 19.99)。
        """
        observe = _sequence([Shift(0.4, -0.2), Shift(0.02, 0.01)])
        adjustor = XYPositionAdjustor(
            observe=observe,
            klipper=klipper,
            stage=stage,
            offset_transform=Identity(),
            tolerance=0.1,
        )

        result = adjustor.adjust()

        assert result.x == pytest.approx(9.98)
        assert result.y == pytest.approx(19.99)
        assert stage.move.call_count == 1
        kwargs = stage.move.call_args.kwargs
        assert kwargs["x"] == pytest.approx(9.6)
        assert kwargs["y"] == pytest.approx(20.2)
        assert klipper.send_gcode.call_count == 1

    def test_offset_transform_rotates_observed_displacement_into_machine_space(
        self, klipper, stage
    ):
        """符号ピン: 観測 (1,0)mm + offset_transform=Rotation(90) → 補正 −(0,1).

        offset = R(o) = (0,1)、target = pos − offset = (10, 19)。 R⁻¹
        を適用する実装はここで割れる。
        """
        observe = _sequence([Shift(1.0, 0.0), Shift(0.0, 0.0)])
        adjustor = XYPositionAdjustor(
            observe=observe,
            klipper=klipper,
            stage=stage,
            offset_transform=Rotation(90.0),
            tolerance=0.1,
        )

        result = adjustor.adjust()

        kwargs = stage.move.call_args.kwargs
        assert kwargs["x"] == pytest.approx(10.0)
        assert kwargs["y"] == pytest.approx(19.0)
        assert result.x == pytest.approx(10.0)
        assert result.y == pytest.approx(20.0)

    def test_rotation_component_of_observed_transform_reduces_to_translation(
        self, klipper, stage
    ):
        """回転成分付き Transform は原点適用で並進へ縮約される.

        ステージは回転補正できないため、observe() の回転成分は apply(Point2d(0,0)) で自然に変位
        (0.3, 0) へ落ちる。
        """
        observe = _sequence(
            [
                Compose([Rotation(90.0), Shift(0.3, 0.0)]),
                Compose([Rotation(90.0), Shift(0.0, 0.0)]),
            ]
        )
        adjustor = XYPositionAdjustor(
            observe=observe,
            klipper=klipper,
            stage=stage,
            offset_transform=Identity(),
            tolerance=0.1,
        )

        adjustor.adjust()

        kwargs = stage.move.call_args.kwargs
        assert kwargs["x"] == pytest.approx(9.7)
        assert kwargs["y"] == pytest.approx(20.0)

    def test_adjust_raises_when_not_converged_within_max_iterations(
        self, klipper, stage
    ):
        """Tolerance 内に収束しない場合は max_iterations 後に RuntimeError."""
        adjustor = XYPositionAdjustor(
            observe=lambda: Shift(1.0, 0.0),
            klipper=klipper,
            stage=stage,
            offset_transform=Identity(),
            tolerance=0.1,
            max_iterations=3,
        )

        with pytest.raises(RuntimeError) as exc:
            adjustor.adjust()

        assert "収束しませんでした" in str(exc.value)
        assert klipper.send_gcode.call_count == 3
