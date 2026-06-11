"""カメラ空間の照合結果を機械座標系の補正Transformへ変換する.

符号規約は実機検証済みのアンカーに従う:

- (A2) ``R = Rotation.from_points(Δs, Δo)`` (offset.py)
- (A4) 補正式 ``target = pos − R.apply(o_mm)`` (position.py)

camera→machine 写像 ψ はアンカー位置 a に対し ``ψ_a(o) = a − R(o)``。
(A4) を再現するよう R をそのまま使う（厳密幾何の R⁻¹ に修正しない）。
"""

from pcbasm.geometry import Compose, Point2d, Scale, Shift, Transform


def _psi(offset_transform: Transform, anchor: Point2d) -> Transform:
    """Camera→machine 写像 ψ_a(o) = a − R(o) を構成する."""
    return Compose(
        [offset_transform, Scale.flip(x=True, y=True), Shift.from_point(anchor)]
    )


def to_machine_transform(
    camera_transform: Transform,
    offset_transform: Transform,
    projection_anchor: Point2d,
    observed_at: Point2d,
) -> Transform:
    """カメラ空間の照合結果を機械座標系の補正Transformへ共役変換する.

    ``M = ψ_{observed_at} ∘ camera_transform ∘ ψ_{projection_anchor}⁻¹``。
    設計machine点を観測machine点へ写す（fill path 合成用）。

    Args:
        camera_transform: 想定→観測のTransform（カメラmm空間、原点=画像中心）
        offset_transform: 観測オフセット系から機械座標系への変換 R
        projection_anchor: 投影アンカーの機械座標 s0（mm）
        observed_at: 最終観測時のステージ位置（機械座標、mm）

    Returns:
        機械座標系の補正Transform M
    """
    return Compose(
        [
            _psi(offset_transform, projection_anchor).inverse(),
            camera_transform,
            _psi(offset_transform, observed_at),
        ]
    )
