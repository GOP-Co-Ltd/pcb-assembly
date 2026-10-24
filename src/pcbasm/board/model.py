"""Pcbnew に依存しない基板データ（外形・部品・パッド・銅箔島）."""

from enum import Enum
from functools import cached_property

import attrs
from shapely import Polygon

from pcbasm.geometry.transform import Point2d


class Layer(Enum):
    """PCB レイヤー."""

    TOP = "Top"
    BOTTOM = "Bottom"


@attrs.frozen
class Outline:
    """基板アウトライン.

    Attributes:
        polygon: 基板外形のポリゴン (mm 単位)
        width: 基板幅 (mm)
        height: 基板高さ (mm)
    """

    polygon: Polygon

    def __attrs_post_init__(self):
        # property を呼び出してキャッシュ
        self.width
        self.height

    @cached_property
    def width(self) -> float:
        """基板幅 (mm)."""
        minx, _, maxx, _ = self.polygon.bounds
        return maxx - minx

    @cached_property
    def height(self) -> float:
        """基板高さ (mm)."""
        _, miny, _, maxy = self.polygon.bounds
        return maxy - miny


@attrs.frozen
class Component:
    """部品情報.

    Attributes:
        designator: 部品リファレンス (例: "U1", "R1")
        value: 部品値 (例: "10k", "100nF")
        package: パッケージ名 (例: "0402", "QFP-48")
        position: footprint 原点の基板座標 (mm)
        rotation: KiCad の footprint 向き (度)
        layer: レイヤー (Top/Bottom)
    """

    designator: str
    value: str
    package: str
    position: Point2d
    rotation: float
    layer: Layer


@attrs.frozen
class Pad:
    """パッド情報.

    Attributes:
        designator: 部品リファレンス (例: "U1", "R1")
        pad_number: パッド番号 (例: "1", "A1")
        net_name: ネット名
        layer: レイヤー (Top/Bottom)
        polygon: ペースト領域のポリゴン (mm 単位)
        is_custom_shape: カスタム形状かどうか
    """

    designator: str
    pad_number: str
    net_name: str
    layer: Layer
    polygon: Polygon
    is_custom_shape: bool = False

    @property
    def center(self) -> Point2d:
        """ペースト領域（``polygon``）の重心を計算."""
        centroid = self.polygon.centroid
        return Point2d(x=centroid.x, y=centroid.y)

    @property
    def area(self) -> float:
        """ポリゴンの面積を計算 (mm^2)."""
        return self.polygon.area


@attrs.frozen
class Copper:
    """電気的・物理的に接続された銅箔島.

    Attributes:
        layer: レイヤー (Top/Bottom)
        polygon: 銅箔島のポリゴン (mm 単位、穴を含む場合あり)
    """

    layer: Layer
    polygon: Polygon

    @property
    def area(self) -> float:
        """ポリゴンの面積を計算 (mm^2)."""
        return self.polygon.area
