"""カメラの写像（pixel ⇄ カメラ mm ⇄ 機械座標）を 1 か所に置く."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import attrs

from pcbasm.geometry import (
    Affine2d,
    Compose,
    Identity,
    Point2d,
    Scale,
    Shift,
    Transform,
)

from .calibration import CameraCalibration
from .image import ImageSize, PixelRect

_FLIP = Scale.flip(x=True, y=True)


@attrs.frozen
class CameraModel:
    """校正とカメラの回転から決まる、カメラの写像.

    カメラ mm 空間は原点が画像中心で、軸の向きは画像と同じ（+x 右、+y 下）。

    画像中心からの pixel のずれは :meth:`pixel_offset_to_camera` でカメラ mm へ写す。

    機械座標との間の写像は、現行の 2 つの向きを別メソッドで持つ。

    - :meth:`observed_to_machine_offset`: 観測オフセット o を機械座標 ``a − R(o)`` へ（位置補正と照合結果の変換）
    - :meth:`machine_to_camera`: 機械座標の点 p をカメラ mm ``R(s − p)`` へ（銅箔の投影）

    2 つは互いの逆ではない（R と R⁻¹ のずれ）。統一は付録 X1。

    ジョブの開始時（と preview の要求時）に、その時点の校正ファイルから :meth:`load` で作る。

    Attributes:
        pixel_per_mm: 1 mm あたりの画素数
        image_size: 校正した画像の大きさ (width, height)
        focus_z: 校正したときの Z [mm]（カメラの焦点が合う高さ）
        calibrated_at: 校正した日時
        rotation: 観測オフセット系から機械座標系への回転 R（位置合わせの開始時に測る）
    """

    pixel_per_mm: float
    image_size: ImageSize
    focus_z: float
    calibrated_at: datetime
    rotation: Transform = attrs.field(factory=Identity)

    @classmethod
    def load(cls, calibration_file: Path) -> CameraModel:
        """校正ファイルから、回転を測る前（恒等）のモデルを作る.

        Raises:
            ValueError: 校正に焦点 Z（z_position）が記録されていない場合
        """
        calibration = CameraCalibration.load(calibration_file)
        if calibration.z_position is None:
            raise ValueError(
                f"校正 {calibration_file.name} に焦点 Z（z_position）がありません。"
                "Klipper につないだ状態で camera_calibration をやり直してください"
            )
        return cls(
            pixel_per_mm=calibration.pixel_per_mm,
            image_size=calibration.resolution,
            focus_z=calibration.z_position,
            calibrated_at=calibration.calibrated_at,
        )

    def with_rotation(self, rotation: Transform) -> CameraModel:
        """計測したカメラの回転を持つモデルを返す."""
        return attrs.evolve(self, rotation=rotation)

    def image_size_error(self, size: ImageSize) -> str | None:
        """撮った画像の大きさが校正の解像度と違えば、その理由を返す."""
        if size == self.image_size:
            return None
        width, height = self.image_size
        return f"カメラ {size[0]}x{size[1]} が校正 {width}x{height} と違います"

    def centered_roi(self, size_px: int) -> PixelRect:
        """画像中心に一辺 ``size_px`` の正方 ROI を返す."""
        if size_px < 1:
            raise ValueError(f"size_pxは1以上である必要があります: {size_px}")
        width, height = self.image_size
        if size_px > min(self.image_size):
            raise ValueError(
                f"size_px {size_px} が画像サイズ {self.image_size} を超えています"
            )
        x0 = (width - size_px) // 2
        y0 = (height - size_px) // 2
        return (x0, y0, x0 + size_px, y0 + size_px)

    def pixel_offset_to_camera(self, offset_px: Point2d) -> Point2d:
        """画像中心からの pixel のずれをカメラ mm へ写す."""
        return Point2d(
            x=offset_px.x / self.pixel_per_mm,
            y=offset_px.y / self.pixel_per_mm,
        )

    def observed_to_machine_offset(self, anchor: Point2d) -> Transform:
        """観測オフセット o [カメラ mm] を機械座標 ``anchor − R(o)`` へ写す.

        ``anchor`` は観測したときのステージ位置。
        """
        return Compose([self.rotation, _FLIP, Shift.from_point(anchor)])

    def machine_to_camera(self, stage: Point2d) -> Transform:
        """ステージが ``stage`` のとき、機械座標の点 p をカメラ mm ``R(stage − p)`` へ写す."""
        return Compose([_FLIP, Shift.from_point(stage), self.rotation])

    def machine_to_pixel(self, stage: Point2d) -> Transform:
        """ステージが ``stage`` のとき、機械座標の点を全画面の pixel 座標へ写す."""
        width, height = self.image_size
        return Compose(
            [
                self.machine_to_camera(stage),
                Scale(self.pixel_per_mm, self.pixel_per_mm),
                Shift(width / 2, height / 2),
            ]
        )

    def board_to_pixel(self, board_transform: Transform, stage: Point2d) -> Affine2d:
        """ステージが ``stage`` のとき、board 座標を pixel 座標へ写すアフィン変換を返す."""
        return Affine2d.from_transform(
            Compose([board_transform, self.machine_to_pixel(stage)])
        )
