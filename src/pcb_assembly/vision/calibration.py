"""カメラキャリブレーション: チェッカーボードからpixel/mm比率を計算."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import attrs
from cattrs.preconf.json import make_converter

# Path の変換をサポートするコンバーター
_converter = make_converter()
_converter.register_unstructure_hook(Path, str)
_converter.register_structure_hook(Path, lambda v, _: Path(v))


@attrs.frozen
class CalibrationResult:
    """キャリブレーション結果."""

    pixel_per_mm: float  # pixel/mm比率
    square_size_mm: float  # チェッカーボードの1マスのサイズ (mm)
    mean_distance_px: float  # 1マスの平均距離 (pixel)
    std_distance_px: float  # 1マスの距離の標準偏差 (pixel)
    pattern_size: tuple[int, int]  # 検出されたパターンサイズ (cols, rows)
    resolution: tuple[int, int]  # カメラ解像度 (width, height)
    crop_size: tuple[int, int]  # 関心領域サイズ (width, height)
    calibrated_at: datetime  # キャリブレーション日時
    image_path: Path  # 保存された画像パス

    @property
    def mm_per_pixel(self) -> float:
        """mm/pixel比率."""
        return 1.0 / self.pixel_per_mm

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換."""
        return _converter.unstructure(self)

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        """辞書から生成."""
        return _converter.structure(data, cls)

    def save(self, path: Path) -> None:
        """JSONファイルに保存."""
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """JSONファイルから読み込み."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
