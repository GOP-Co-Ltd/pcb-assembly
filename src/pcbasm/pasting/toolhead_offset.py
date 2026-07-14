"""ツールヘッドオフセットの計測（パージ痕の円検出・補正量検証）と計測結果."""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import attrs
from cattrs.preconf.json import make_converter

from pcbasm import gcode
from pcbasm.geometry import Point2d, Transform
from pcbasm.hal import Camera, Klipper, XYZStage
from pcbasm.posctrl import OffsetObserver, XYPositionAdjustor
from pcbasm.vision import CalibrationResult, CircleDetector, FrameSink

_converter = make_converter()
_converter.register_unstructure_hook(Point2d, lambda p: {"x": p.x, "y": p.y})
_converter.register_structure_hook(Point2d, lambda d, _: Point2d(x=d["x"], y=d["y"]))


@attrs.frozen
class ToolheadOffsetResult:
    """ツールヘッドオフセット計測結果.

    Attributes:
        offset: ツールヘッドXYオフセット (mm)
        dispense_position: ペースト吐出時のステージ位置
        camera_position: カメラが検出した位置
        tolerance: 収束許容誤差 (mm)
        calibrated_at: 計測日時
    """

    offset: Point2d
    dispense_position: Point2d
    camera_position: Point2d
    tolerance: float
    calibrated_at: datetime

    @classmethod
    def measure(
        cls,
        *,
        dispense_position: Point2d,
        camera_position: Point2d,
        tolerance: float,
        calibrated_at: datetime,
    ) -> Self:
        """吐出位置とカメラ検出位置からオフセットを算出して構築する.

        ``offset = dispense_position - camera_position``（カメラ検出位置に
        対するツールヘッドの XY オフセット）。
        """
        return cls(
            offset=dispense_position - camera_position,
            dispense_position=dispense_position,
            camera_position=camera_position,
            tolerance=tolerance,
            calibrated_at=calibrated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換."""
        return _converter.unstructure(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """辞書から生成."""
        return _converter.structure(data, cls)

    def save(self, path: Path) -> None:
        """JSONファイルに保存."""
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """JSONファイルから読み込み."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def locate_paste_blob(
    *,
    camera: Camera,
    klipper: Klipper,
    stage: XYZStage,
    calibration: CalibrationResult,
    offset_transform: Transform,
    crop_size: tuple[int, int],
    camera_position: Point2d,
    diameter_min: float,
    diameter_max: float,
    tolerance: float,
    frame_sink: FrameSink | None = None,
    settle_time: float = 1.0,
) -> Point2d:
    """カメラをパージ痕へ移動して円検出し、収束後の最終カメラ位置を返す.

    Args:
        camera: 撮像に使うカメラ
        klipper: Klipperクライアント
        stage: XYZステージ
        calibration: カメラキャリブレーション結果（pixel_per_mm / z_position）
        offset_transform: 観測オフセット系から機械座標系への変換
        crop_size: 関心領域サイズ (width, height)
        camera_position: パージ痕を視野に収めるカメラ移動先（機械座標）
        diameter_min: 検出円の最小直径 [mm]
        diameter_max: 検出円の最大直径 [mm]
        tolerance: 位置合わせの収束許容誤差 [mm]
        frame_sink: 検出注釈画像を送る sink。Noneの場合は送らない
        settle_time: カメラ移動後の安定待機時間 [sec]

    Raises:
        RuntimeError: 円検出失敗・収束失敗の場合
    """
    klipper.send_gcode(
        stage.move(x=camera_position.x, y=camera_position.y, z=calibration.z_position)
        + gcode.wait_for_done()
    )
    time.sleep(settle_time)
    detector = CircleDetector(
        pixel_per_mm=calibration.pixel_per_mm,
        target_diameter_mm=(diameter_min + diameter_max) / 2,
        crop_size=crop_size,
        diameter_tolerance_mm=(diameter_max - diameter_min) / 2,
    )
    observer = OffsetObserver(
        detector=detector,
        camera=camera,
        crop_size=crop_size,
        frame_sink=frame_sink,
    )
    adjustor = XYPositionAdjustor(
        observe=observer.observe,
        klipper=klipper,
        stage=stage,
        offset_transform=offset_transform,
        tolerance=tolerance,
    )
    return adjustor.adjust()


def validate_offset_correction(
    measured: Point2d, current: Point2d, max_correction: float
) -> str | None:
    """計測オフセットと現行設定の差を検証し、不正なら日本語エラー文を返す.

    Args:
        measured: 計測したツールヘッドオフセット
        current: 現行設定のツールヘッドオフセット
        max_correction: 許容する補正量の上限 [mm]（境界値ちょうどは許容）

    Returns:
        差の距離が ``max_correction`` を超えるときエラー文、正常なら ``None``
    """
    correction = (measured - current).norm
    if correction > max_correction:
        return (
            f"オフセット補正量 {correction:.4f} mm が"
            f"上限 {max_correction:.4f} mm を超えています"
            "（パージ痕の誤検出の可能性）"
        )
    return None
