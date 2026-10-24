"""カメラ空間の照合結果を機械座標系の補正 Transform へ変換する.

符号規約は、実機で検証済みの次のアンカーに従う。

- (A2) ``R = Rotation.from_points(Δs, Δo)`` (camera_rotation.py)
- (A4) 補正式 ``target = pos − R.apply(o_mm)`` (position.py)

camera→machine 写像 ψ はアンカー位置 a に対し ``ψ_a(o) = a − R(o)``（:meth:`CameraModel.observed_to_machine_offset`）。
(A4) を再現するよう R をそのまま使う（厳密幾何の R⁻¹ に修正しない）。
"""

from pcbasm.geometry import Compose, Point2d, Transform
from pcbasm.vision import CameraModel


def to_machine_transform(
    camera_transform: Transform,
    camera_model: CameraModel,
    projection_anchor: Point2d,
    observed_at: Point2d,
) -> Transform:
    """カメラ空間の照合結果を機械座標系の補正 Transform へ共役変換する.

    ``M = ψ_{observed_at} ∘ camera_transform ∘ ψ_{projection_anchor}⁻¹``。
    設計上の machine 点を観測した machine 点へ写す。fill path の合成に使う。

    Args:
        camera_transform: 想定→観測の Transform（カメラ mm 空間、原点=画像中心）
        camera_model: カメラの写像（回転 R を持つ）
        projection_anchor: 投影アンカーの機械座標 s0（mm）
        observed_at: 最終観測時のステージ位置（機械座標、mm）

    Returns:
        機械座標系の補正 Transform M
    """
    return Compose(
        [
            camera_model.observed_to_machine_offset(projection_anchor).inverse(),
            camera_transform,
            camera_model.observed_to_machine_offset(observed_at),
        ]
    )
