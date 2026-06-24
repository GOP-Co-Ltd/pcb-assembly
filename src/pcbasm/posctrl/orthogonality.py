"""board_transform から直行性指標を導出する純粋計算."""

from __future__ import annotations

import math

import attrs

from pcbasm.geometry import Point2d, Transform


@attrs.frozen
class OrthogonalityMetrics:
    """board_transform から導出した直行性指標.

    Attributes:
        axis_angle_error_deg: 変換後の X/Y 軸間角の 90° からのずれ（deg）
        scale_x: X 単位ベクトルの変換後の長さ |T(1,0)-T(0,0)|
        scale_y: Y 単位ベクトルの変換後の長さ |T(0,1)-T(0,0)|
    """

    axis_angle_error_deg: float
    scale_x: float
    scale_y: float

    @classmethod
    def from_transform(cls, transform: Transform) -> OrthogonalityMetrics:
        """board_transform（3 点法計測の affine）から直行性指標を導出する.

        軸間角は変換後の X/Y 単位ベクトルのなす角（[0, 180] deg、鏡映の影響を 受けない）とし、90°
        からのずれを返す。
        """
        origin = transform.apply(Point2d(0.0, 0.0))
        axis_x = transform.apply(Point2d(1.0, 0.0)) - origin
        axis_y = transform.apply(Point2d(0.0, 1.0)) - origin
        cross = axis_x.x * axis_y.y - axis_x.y * axis_y.x
        dot = axis_x.x * axis_y.x + axis_x.y * axis_y.y
        angle_deg = math.degrees(math.atan2(abs(cross), dot))
        return cls(
            axis_angle_error_deg=angle_deg - 90.0,
            scale_x=axis_x.norm,
            scale_y=axis_y.norm,
        )
