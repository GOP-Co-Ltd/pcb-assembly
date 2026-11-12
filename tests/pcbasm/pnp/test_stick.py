"""`pcbasm.pnp.stick` の仕様テスト.

スティックの入力（-1〜1）を XYZ の速度指令にする純粋な変換。

- XY は radial のデッドゾーン。中心からの距離が d 以下なら 0、外は境界で 0 から連続になるよう
  スケールし直す。倒し切った大きさ（1 を超えたら 1 に収める）で 1 になり、向きは保つ
- Z は 1 次元のデッドゾーンで、同じようにスケールし直す
- スティックの座標は「右 = +x、上 = +y、上 = +z」。機体の座標は「右 = X+、上 = Y−、上 = Z+」
- 1 で各軸の最高速度
"""

import math

import pytest

from pcbasm.hal import Velocity
from pcbasm.pnp.stick import StickInput, axial_deadzone, radial_deadzone, stick_velocity

MAX = Velocity(x=100.0, y=80.0, z=20.0)
# 実機のスティックが中央に戻ったときに残る値（d=0.15 で 0 に落とす）
RESTING = 0.0213


class TestRadialDeadzone:
    @pytest.mark.parametrize(
        ("x", "y"), [(RESTING, RESTING), (-RESTING, RESTING), (0.0, 0.0), (0.15, 0.0)]
    )
    def test_inside_deadzone_is_zero(self, x: float, y: float) -> None:
        assert radial_deadzone(x, y, 0.15) == (0.0, 0.0)

    @pytest.mark.parametrize(
        ("x", "y"), [(1.0, 0.0), (0.0, -1.0), (math.sqrt(0.5), math.sqrt(0.5))]
    )
    def test_full_tilt_is_unit_length(self, x: float, y: float) -> None:
        sx, sy = radial_deadzone(x, y, 0.15)
        assert math.hypot(sx, sy) == pytest.approx(1.0)
        assert (sx, sy) == pytest.approx((x, y))

    def test_corner_is_clamped_to_unit_and_keeps_direction(self) -> None:
        sx, sy = radial_deadzone(1.0, -1.0, 0.15)
        assert math.hypot(sx, sy) == pytest.approx(1.0)
        assert math.atan2(sy, sx) == pytest.approx(math.atan2(-1.0, 1.0))

    def test_keeps_direction_between_deadzone_and_edge(self) -> None:
        sx, sy = radial_deadzone(0.3, 0.4, 0.15)
        # 距離 0.5 → (0.5 - 0.15) / 0.85
        assert math.hypot(sx, sy) == pytest.approx(0.35 / 0.85)
        assert math.atan2(sy, sx) == pytest.approx(math.atan2(0.4, 0.3))

    def test_is_continuous_at_boundary(self) -> None:
        sx, sy = radial_deadzone(0.15 + 1e-9, 0.0, 0.15)
        assert math.hypot(sx, sy) == pytest.approx(0.0, abs=1e-8)

    @pytest.mark.parametrize(
        ("x", "y"), [(0.0213, -0.0213), (0.3, 0.4), (-0.6, 0.8), (1.0, 0.0)]
    )
    def test_zero_deadzone_passes_through(self, x: float, y: float) -> None:
        assert radial_deadzone(x, y, 0.0) == pytest.approx((x, y))


class TestAxialDeadzone:
    @pytest.mark.parametrize("value", [RESTING, -RESTING, 0.0, 0.15, -0.15])
    def test_inside_deadzone_is_zero(self, value: float) -> None:
        assert axial_deadzone(value, 0.15) == 0.0

    @pytest.mark.parametrize(
        ("value", "expected"), [(1.0, 1.0), (-1.0, -1.0), (0.5, 0.35 / 0.85)]
    )
    def test_rescales_outside_deadzone(self, value: float, expected: float) -> None:
        assert axial_deadzone(value, 0.15) == pytest.approx(expected)

    def test_is_continuous_at_boundary(self) -> None:
        assert axial_deadzone(-(0.15 + 1e-9), 0.15) == pytest.approx(0.0, abs=1e-8)

    @pytest.mark.parametrize("value", [-1.0, -0.0213, 0.0, 0.5, 1.0])
    def test_zero_deadzone_passes_through(self, value: float) -> None:
        assert axial_deadzone(value, 0.0) == pytest.approx(value)


class TestStickVelocity:
    def test_neutral_is_zero(self) -> None:
        assert stick_velocity(StickInput(), MAX) == Velocity()

    @pytest.mark.parametrize(
        ("stick", "expected"),
        [
            (StickInput(x=1.0), Velocity(x=100.0)),  # 右 = X+
            (StickInput(x=-1.0), Velocity(x=-100.0)),
            (StickInput(y=1.0), Velocity(y=-80.0)),  # 上 = Y−
            (StickInput(y=-1.0), Velocity(y=80.0)),
            (StickInput(z=1.0), Velocity(z=20.0)),  # 上 = Z+（上昇）
            (StickInput(z=-1.0), Velocity(z=-20.0)),
        ],
    )
    def test_full_tilt_is_max_velocity_in_machine_axes(
        self, stick: StickInput, expected: Velocity
    ) -> None:
        assert stick_velocity(stick, MAX, deadzone_xy=0.15, deadzone_z=0.15) == expected

    def test_resting_values_do_not_move(self) -> None:
        stick = StickInput(x=RESTING, y=-RESTING, z=RESTING)
        assert (
            stick_velocity(stick, MAX, deadzone_xy=0.15, deadzone_z=0.15) == Velocity()
        )

    def test_default_deadzone_is_zero(self) -> None:
        velocity = stick_velocity(StickInput(x=RESTING, y=RESTING, z=-RESTING), MAX)
        assert (velocity.x, velocity.y, velocity.z) == pytest.approx(
            (RESTING * 100.0, -RESTING * 80.0, -RESTING * 20.0)
        )

    def test_diagonal_does_not_exceed_max(self) -> None:
        velocity = stick_velocity(StickInput(x=1.0, y=1.0), MAX, deadzone_xy=0.15)
        assert math.hypot(velocity.x / MAX.x, velocity.y / MAX.y) == pytest.approx(1.0)
        assert velocity.x > 0
        assert velocity.y < 0

    def test_xy_and_z_deadzones_are_independent(self) -> None:
        stick = StickInput(x=0.2, z=0.2)
        velocity = stick_velocity(stick, MAX, deadzone_xy=0.3, deadzone_z=0.1)
        assert velocity.x == 0.0
        assert velocity.z == pytest.approx(0.1 / 0.9 * 20.0)
