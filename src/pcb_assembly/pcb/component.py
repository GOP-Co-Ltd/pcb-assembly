"""部品情報のデータクラスとCSV操作."""

import csv
from collections import UserList
from pathlib import Path
from typing import Self

import attrs

from .utils import Layer


@attrs.frozen
class Component:
    """部品情報.

    Attributes:
        designator: 部品リファレンス (例: "U1", "R1")
        value: 部品値 (例: "10k", "100nF")
        package: パッケージ名 (例: "0402", "QFP-48")
        x: X座標 (mm)
        y: Y座標 (mm)
        rotation: 回転角度 (度)
        layer: レイヤー (Top/Bottom)
    """

    designator: str
    value: str
    package: str
    x: float
    y: float
    rotation: float
    layer: Layer

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> Self:
        """CSVの行から生成."""
        return cls(
            designator=row["Designator"],
            value=row["Value"],
            package=row["Package"],
            x=float(row["X"]),
            y=float(row["Y"]),
            rotation=float(row["Rotation"]),
            layer=Layer(row["Layer"]),
        )

    def to_csv_row(self) -> list[str]:
        """CSV出力用の行を生成."""
        return [
            self.designator,
            self.value,
            self.package,
            f"{self.x:.4f}",
            f"{self.y:.4f}",
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
