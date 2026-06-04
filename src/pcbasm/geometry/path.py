from __future__ import annotations

from collections.abc import Iterator

import attrs

from .transform import Point3d, Transform


@attrs.frozen
class Path:
    """順序付き3D点列を表すイミュータブルなクラス.

    速度・タイミングを持たない純粋な幾何であり、座標の並びのみを保持する。

    Attributes:
        points: 経路を構成する点列
    """

    points: tuple[Point3d, ...] = attrs.field(converter=tuple)

    def length(self) -> float:
        """経路の総距離を返す.

        連続する点の差ベクトルのノルムを合計する。点が2未満の場合は0.0を返す。

        Returns:
            経路の総距離
        """
        return sum(
            (self.points[i + 1] - self.points[i]).norm()
            for i in range(len(self.points) - 1)
        )

    def transformed(self, transform: Transform) -> Path:
        """全点に変換を適用した新しいPathを返す.

        Args:
            transform: 各点に適用する変換

        Returns:
            変換後の点列を持つPathインスタンス
        """
        return Path(transform.apply(p) for p in self.points)

    def __len__(self) -> int:
        return len(self.points)

    def __iter__(self) -> Iterator[Point3d]:
        return iter(self.points)

    def __getitem__(self, index: int) -> Point3d:
        return self.points[index]
