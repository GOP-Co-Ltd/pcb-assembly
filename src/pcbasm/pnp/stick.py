"""スティックの入力を XYZ の速度指令にする.

スティックの値は -1〜1 で、:class:`StickInput` は「右 = +x、上 = +y、上 = +z」のスティックの座標で持つ。
:func:`stick_velocity` が、機体の座標（右 = X+、上 = Y−、上 = Z+）の速度 [mm/s] に直す。1 が各軸の最高速度。

実機のスティックは中央に戻りきらない（0.02 程度が残る）ので、デッドゾーンを掛ける。XY は 2 軸をまとめた
radial、Z は 1 次元。どちらもデッドゾーンの外を 0〜1 にスケールし直し、境界で速度が飛ばないようにする。
"""

from __future__ import annotations

import math

import attrs

from pcbasm.hal import Velocity


@attrs.frozen
class StickInput:
    """スティックの倒し具合（-1〜1。右 = +x、上 = +y、上 = +z）."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


def radial_deadzone(x: float, y: float, deadzone: float) -> tuple[float, float]:
    """XY の中心からの距離にデッドゾーンを掛ける.

    距離が deadzone 以下なら 0。外は向きを保ったまま、距離を deadzone〜1 から 0〜1 へスケールし直す。
    斜めに倒し切ると距離が 1 を超えるので、1 に収める。
    """
    distance = math.hypot(x, y)
    if distance <= deadzone:
        return 0.0, 0.0
    scale = (min(distance, 1.0) - deadzone) / (1.0 - deadzone) / distance
    return x * scale, y * scale


def axial_deadzone(value: float, deadzone: float) -> float:
    """1 次元の値にデッドゾーンを掛ける（外は deadzone〜1 から 0〜1 へスケールし直す）."""
    magnitude = abs(value)
    if magnitude <= deadzone:
        return 0.0
    return math.copysign((min(magnitude, 1.0) - deadzone) / (1.0 - deadzone), value)


def stick_velocity(
    stick: StickInput,
    max_velocity: Velocity,
    *,
    deadzone_xy: float = 0.0,
    deadzone_z: float = 0.0,
) -> Velocity:
    """スティックの入力を機体の座標の速度 [mm/s] にする.

    Args:
        stick: スティックの座標の入力
        max_velocity: 倒し切ったとき（1）の各軸の速度（:attr:`~pcbasm.hal.MotionStage.max_velocity`）
        deadzone_xy: XY の radial のデッドゾーン（0〜0.5）
        deadzone_z: Z のデッドゾーン（0〜0.5）
    """
    x, y = radial_deadzone(stick.x, stick.y, deadzone_xy)
    z = axial_deadzone(stick.z, deadzone_z)
    # スティックの上（+y）は機体の Y−
    return Velocity(x=x * max_velocity.x, y=-y * max_velocity.y, z=z * max_velocity.z)
