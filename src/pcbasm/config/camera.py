"""[camera] 節：カメラの撮像条件と校正ファイル."""

from pathlib import Path

import attrs


@attrs.frozen
class CameraCropConfig:
    """カメラのクロップ設定."""

    width: int
    height: int

    @property
    def size(self) -> tuple[int, int]:
        """クロップサイズを (width, height) のタプルで返す."""
        return (self.width, self.height)


@attrs.frozen
class CameraConfig:
    """カメラの設定."""

    width: int
    height: int
    fps: float
    crop: CameraCropConfig
    calibration_file: Path
    device_id: int = 0
    format: str = "YUYV"
    backend: str = "csi"
