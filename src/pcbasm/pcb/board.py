"""PCB要素のモジュール: 部品・パッド情報とCSV/JSON操作."""

import csv
import json
from collections import UserList
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Any, Self

import attrs
from cattrs.preconf.json import make_converter
from shapely import Polygon

from pcbasm.geometry.transform import Point2d


class Layer(Enum):
    """PCBレイヤー."""

    TOP = "Top"
    BOTTOM = "Bottom"


# cattrs コンバーター設定
_converter = make_converter()
_converter.register_unstructure_hook(Layer, lambda v: v.value)
_converter.register_structure_hook(Layer, lambda v, _: Layer(v))
_converter.register_unstructure_hook(
    Polygon,
    lambda p: {
        "exterior": list(p.exterior.coords),
        "holes": [list(interior.coords) for interior in p.interiors],
    },
)
_converter.register_structure_hook(
    Polygon,
    lambda v, _: Polygon(v["exterior"], holes=v.get("holes", [])),
)


@attrs.frozen
class Outline:
    """基板アウトライン.

    Attributes:
        polygon: 基板外形のポリゴン (mm単位)
        width: 基板幅 (mm)
        height: 基板高さ (mm)
    """

    polygon: Polygon

    def __attrs_post_init__(self):
        # propertyを呼び出してキャッシュ
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

    def save(self, path: Path) -> None:
        """JSONファイルに保存（外周だけを保存し、穴は保存しない）."""
        data = {"polygon": list(self.polygon.exterior.coords)}
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """JSONファイルから読み込み."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(Polygon(data["polygon"]))


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

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> Self:
        """CSVの行から生成."""
        return cls(
            designator=row["Designator"],
            value=row["Value"],
            package=row["Package"],
            position=Point2d(x=float(row["X"]), y=float(row["Y"])),
            rotation=float(row["Rotation"]),
            layer=Layer(row["Layer"]),
        )

    def to_csv_row(self) -> list[str]:
        """CSV出力用の行を生成."""
        return [
            self.designator,
            self.value,
            self.package,
            f"{self.position.x:.4f}",
            f"{self.position.y:.4f}",
            f"{self.rotation:.2f}",
            self.layer.value,
        ]


PNP_CSV_HEADER: tuple[str, ...] = (
    "Designator",
    "Value",
    "Package",
    "X",
    "Y",
    "Rotation",
    "Layer",
)


class ComponentList(UserList[Component]):
    """部品情報のリスト.

    Pick and Place CSVファイルの読み書きをサポート.

    Example:
        >>> components = ComponentList.load(Path("board_pnp.csv"))
        >>> components.save(Path("output_pnp.csv"))
    """

    def nearest(self, point: Point2d) -> Component:
        """指定座標に最も近い部品を取得.

        Args:
            point: 基準座標

        Returns:
            最も近い部品

        Raises:
            ValueError: リストが空の場合
        """
        if not self.data:
            raise ValueError("ComponentList is empty")
        return min(self.data, key=lambda c: (c.position - point).norm)

    def save(self, path: Path) -> None:
        """Pick and Place CSVファイルに保存."""
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(PNP_CSV_HEADER)
            for comp in self.data:
                writer.writerow(comp.to_csv_row())

    @classmethod
    def load(cls, path: Path) -> Self:
        """Pick and Place CSVファイルから読み込み."""
        components = cls()
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                components.append(Component.from_csv_row(row))
        return components


@attrs.frozen
class Pad:
    """パッド情報.

    Attributes:
        designator: 部品リファレンス (例: "U1", "R1")
        pad_number: パッド番号 (例: "1", "A1")
        net_name: ネット名
        layer: レイヤー (Top/Bottom)
        polygon: ペースト領域のポリゴン (mm単位)
        copper_polygon: 実銅箔領域のポリゴン (mm単位)。省略時はpolygonと同一
        is_custom_shape: カスタム形状かどうか
    """

    designator: str
    pad_number: str
    net_name: str
    layer: Layer
    polygon: Polygon
    copper_polygon: Polygon = attrs.Factory(lambda self: self.polygon, takes_self=True)
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

    def nearest(self, point: Point2d) -> Pad:
        """指定座標に最も近いパッドを取得.

        Args:
            point: 基準座標

        Returns:
            最も近いパッド

        Raises:
            ValueError: リストが空の場合
        """
        if not self.data:
            raise ValueError("PadList is empty")
        return min(self.data, key=lambda p: (p.center - point).norm)

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


@attrs.frozen
class Copper:
    """電気的・物理的に接続された銅箔島.

    Attributes:
        layer: レイヤー (Top/Bottom)
        polygon: 銅箔島のポリゴン (mm単位、穴を含む場合あり)
    """

    layer: Layer
    polygon: Polygon

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


class CopperList(UserList[Copper]):
    """銅箔島情報のリスト.

    JSONファイルの読み書きをサポート.

    Example:
        >>> coppers = CopperList.load(Path("board_copper.json"))
        >>> coppers.save(Path("output_copper.json"))
    """

    def save(self, path: Path) -> None:
        """JSONファイルに保存."""
        data = [copper.to_dict() for copper in self.data]
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """JSONファイルから読み込み."""
        data = json.loads(path.read_text(encoding="utf-8"))
        coppers = cls()
        for item in data:
            coppers.append(Copper.from_dict(item))
        return coppers
