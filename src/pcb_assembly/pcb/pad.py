"""パッド情報のデータクラスとJSON操作."""

import json
from collections import UserList
from pathlib import Path
from typing import Any, Self

import attrs
from cattrs.preconf.json import make_converter
from shapely import Polygon

from .utils import Layer

# cattrs コンバーター設定
_converter = make_converter()
_converter.register_unstructure_hook(Layer, lambda v: v.value)
_converter.register_structure_hook(Layer, lambda v, _: Layer(v))
_converter.register_unstructure_hook(Polygon, lambda p: list(p.exterior.coords))
_converter.register_structure_hook(Polygon, lambda v, _: Polygon(v))


@attrs.frozen
class Pad:
    """パッド情報.

    Attributes:
        designator: 部品リファレンス (例: "U1", "R1")
        pad_number: パッド番号 (例: "1", "A1")
        net_name: ネット名
        layer: レイヤー (Top/Bottom)
        polygon: ペースト領域のポリゴン (mm単位)
        is_custom_shape: カスタム形状かどうか
    """

    designator: str
    pad_number: str
    net_name: str
    layer: Layer
    polygon: Polygon
    is_custom_shape: bool = False

    @property
    def center(self) -> tuple[float, float]:
        """ポリゴンの重心を計算."""
        centroid = self.polygon.centroid
        return (centroid.x, centroid.y)

    @property
    def area(self) -> float:
        """ポリゴンの面積を計算 (mm^2)."""
        return self.polygon.area

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換."""
        return _converter.unstructure(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """辞書から生成."""
        return _converter.structure(data, cls)


class PadList(UserList[Pad]):
    """パッド情報のリスト.

    JSONファイルの読み書きをサポート.

    Example:
        >>> pads = PadList.load(Path("board_pads.json"))
        >>> pads.save(Path("output_pads.json"))
    """

    def save(self, path: Path) -> None:
        """JSONファイルに保存."""
        data = [pad.to_dict() for pad in self.data]
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """JSONファイルから読み込み."""
        data = json.loads(path.read_text(encoding="utf-8"))
        pads = cls()
        for item in data:
            pads.append(Pad.from_dict(item))
        return pads
