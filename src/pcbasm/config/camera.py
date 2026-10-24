"""[camera] 節：カメラの撮像条件と校正ファイル."""

from pathlib import Path

import attrs

from pcbasm.config.fields import setting
from pcbasm.validators import as_validator, validate_positive_int

_positive_int = as_validator(validate_positive_int)


@attrs.frozen
class CameraCropConfig:
    """カメラのクロップ設定."""

    width: int = attrs.field(
        validator=_positive_int,
        metadata=setting(
            label="クロップ幅", unit="px", group="クロップ", order=7, minimum=1
        ),
    )
    height: int = attrs.field(
        validator=_positive_int,
        metadata=setting(
            label="クロップ高さ", unit="px", group="クロップ", order=8, minimum=1
        ),
    )

    @property
    def size(self) -> tuple[int, int]:
        """クロップサイズを (width, height) のタプルで返す."""
        return (self.width, self.height)


@attrs.frozen
class CameraConfig:
    """カメラの設定."""

    width: int = attrs.field(
        metadata=setting(label="幅", unit="px", group="デバイス", order=3)
    )
    height: int = attrs.field(
        metadata=setting(label="高さ", unit="px", group="デバイス", order=4)
    )
    fps: float = attrs.field(
        metadata=setting(label="フレームレート", unit="fps", group="デバイス", order=5)
    )
    crop: CameraCropConfig
    calibration_file: Path = attrs.field(
        metadata=setting(label="キャリブレーションファイル", group="デバイス", order=1)
    )
    device_id: int = attrs.field(
        default=0, metadata=setting(label="デバイスID", group="デバイス", order=2)
    )
    format: str = attrs.field(
        default="YUYV",
        metadata=setting(label="ピクセルフォーマット", group="デバイス", order=6),
    )
    backend: str = "csi"
