"""ツールヘッドオフセット計測結果."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import attrs
from cattrs.preconf.json import make_converter

from pcbasm.geometry import Point2d

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
