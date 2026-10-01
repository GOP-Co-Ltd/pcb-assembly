"""2D/3D の点と、点に作用する変換.

単位は呼び出し側の座標系に従う（本プロジェクトでは mm）。

変換はすべてイミュータブルで、``apply`` は新しい点を返す。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import UserList
from collections.abc import Iterable
from typing import Self, overload, override

import attrs
import numpy as np
import numpy.typing as npt


@attrs.frozen
class Point3d:
    """3 次元空間の座標を表すイミュータブルなクラス.

    長さは ``norm()`` のメソッド呼び出しで得る（:class:`Point2d` はプロパティ）。

    Attributes:
        x: X 座標
        y: Y 座標
        z: Z 座標
    """

    x: float
    y: float
    z: float

    def __add__(self, other: Self) -> Self:
        return self.__class__(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: Self) -> Self:
        return self.__class__(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, scalar: float) -> Self:
        return self.__class__(self.x * scalar, self.y * scalar, self.z * scalar)

    def __rmul__(self, scalar: float) -> Self:
        return self.__mul__(scalar)

    def __truediv__(self, scalar: float) -> Self:
        return self.__class__(self.x / scalar, self.y / scalar, self.z / scalar)

    def norm(self) -> float:
        """ベクトルのノルム（長さ）を返す."""
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def to2d(self) -> Point2d:
        """Point2d 型に変換する（z 座標は無視）."""
        return Point2d(x=self.x, y=self.y)

    @classmethod
    def zero(cls, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> Self:
        """指定されていない軸を 0 で初期化した Point3d を返す."""
        return cls(x, y, z)


@attrs.frozen
class Point2d:
    """2 次元空間の座標を表すイミュータブルなクラス.

    長さは ``norm`` のプロパティで得る（:class:`Point3d` はメソッド）。

    Attributes:
        x: X 座標
        y: Y 座標
    """

    x: float
    y: float

    def __add__(self, other: Self) -> Self:
        return self.__class__(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Self) -> Self:
        return self.__class__(self.x - other.x, self.y - other.y)

    def __mul__(self, scalar: float) -> Self:
        return self.__class__(self.x * scalar, self.y * scalar)

    def __rmul__(self, scalar: float) -> Self:
        return self.__mul__(scalar)

    def __truediv__(self, scalar: float) -> Self:
        return self.__class__(self.x / scalar, self.y / scalar)

    @property
    def norm(self) -> float:
        """ベクトルのノルム（長さ）を返す."""
        return math.sqrt(self.x**2 + self.y**2)

    def to3d(self, z: float = 0.0) -> Point3d:
        """Point3d 型に変換する.

        Args:
            z: Z 座標（デフォルト: 0.0）

        Returns:
            Point3d インスタンス
        """
        return Point3d(x=self.x, y=self.y, z=z)


type Point = Point2d | Point3d


class Transform(ABC):
    """変換の抽象基底クラス."""

    @overload
    def apply(self, point: Point2d) -> Point2d: ...

    @overload
    def apply(self, point: Point3d) -> Point3d: ...

    @abstractmethod
    def apply(self, point: Point) -> Point:
        """点に変換を適用する."""
        ...

    @abstractmethod
    def inverse(self) -> Self:
        """逆変換を返す."""
        ...


@attrs.frozen
class Scale(Transform):
    """3 次元スケール変換を表すイミュータブルなクラス.

    Attributes:
        x: X 軸方向のスケール係数
        y: Y 軸方向のスケール係数
        z: Z 軸方向のスケール係数
    """

    x: float = 1.0
    y: float = 1.0
    z: float = 1.0

    @overload
    def apply(self, point: Point2d) -> Point2d: ...

    @overload
    def apply(self, point: Point3d) -> Point3d: ...

    @override
    def apply(self, point: Point) -> Point:
        """点にスケール変換を適用する."""
        if isinstance(point, Point2d):
            return Point2d(point.x * self.x, point.y * self.y)
        return Point3d(point.x * self.x, point.y * self.y, point.z * self.z)

    @override
    def inverse(self) -> Self:
        """逆スケール変換を返す.

        Returns:
            各軸の逆数をスケール係数とする Scale インスタンス
        """
        return self.__class__(1 / self.x, 1 / self.y, 1 / self.z)

    @classmethod
    def flip(cls, x: bool = False, y: bool = False, z: bool = False) -> Self:
        """指定した軸を反転するスケールを返す.

        Args:
            x: True の場合、X 軸を反転
            y: True の場合、Y 軸を反転
            z: True の場合、Z 軸を反転

        Returns:
            指定軸が反転された Scale インスタンス
        """
        return cls(
            -1.0 if x else 1.0,
            -1.0 if y else 1.0,
            -1.0 if z else 1.0,
        )


@attrs.frozen
class Rotation(Transform):
    """Z 軸周りの回転を表すイミュータブルなクラス.

    正の角度は +X 軸を +Y 軸へ向ける向きに回す。

    Y 上向きの座標系では反時計回り、基板座標のような Y 下向きの座標系では画面上で時計回りになる。

    Attributes:
        degrees: 回転角度 [度]
    """

    degrees: float = 0.0

    @property
    def radians(self) -> float:
        """回転角度をラジアンで返す."""
        return math.radians(self.degrees)

    @overload
    def apply(self, point: Point2d) -> Point2d: ...

    @overload
    def apply(self, point: Point3d) -> Point3d: ...

    @override
    def apply(self, point: Point) -> Point:
        """点に回転変換を適用する."""
        c = math.cos(self.radians)
        s = math.sin(self.radians)
        new_x = point.x * c - point.y * s
        new_y = point.x * s + point.y * c
        if isinstance(point, Point2d):
            return Point2d(new_x, new_y)
        return Point3d(new_x, new_y, point.z)

    @override
    def inverse(self) -> Self:
        """逆回転を返す.

        Returns:
            反対方向に同じ角度だけ回転する Rotation インスタンス
        """
        return self.__class__(-self.degrees)

    @classmethod
    def from_points(cls, base: Point2d, target: Point2d) -> Self:
        """2 つのベクトル間の角度から Rotation を生成する.

        ベクトルの長さは無視し、向きだけを使う。

        Args:
            base: 基準ベクトル
            target: 対象ベクトル

        Returns:
            base の向きを target の向きへ回す Rotation（角度は -180 超 180 以下）
        """
        dot = base.x * target.x + base.y * target.y
        cross = base.x * target.y - base.y * target.x
        radians = math.atan2(cross, dot)
        return cls(degrees=math.degrees(radians))


@attrs.frozen
class Shift(Transform):
    """平行移動変換を表すイミュータブルなクラス.

    Attributes:
        x: X 軸方向の移動量
        y: Y 軸方向の移動量
        z: Z 軸方向の移動量
    """

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    @overload
    def apply(self, point: Point2d) -> Point2d: ...

    @overload
    def apply(self, point: Point3d) -> Point3d: ...

    @override
    def apply(self, point: Point) -> Point:
        """点に平行移動を適用する."""
        if isinstance(point, Point2d):
            return Point2d(point.x + self.x, point.y + self.y)
        return Point3d(point.x + self.x, point.y + self.y, point.z + self.z)

    @override
    def inverse(self) -> Self:
        """逆平行移動を返す.

        Returns:
            反対方向に同じ量だけ移動する Shift インスタンス
        """
        return self.__class__(-self.x, -self.y, -self.z)

    @classmethod
    def from_point(cls, point: Point) -> Self:
        """Point から Shift を生成する."""
        if isinstance(point, Point2d):
            return cls(point.x, point.y, 0.0)
        return cls(point.x, point.y, point.z)


class Identity(Transform):
    """恒等変換（何もしない変換）を表すクラス."""

    @overload
    def apply(self, point: Point2d) -> Point2d: ...

    @overload
    def apply(self, point: Point3d) -> Point3d: ...

    @override
    def apply(self, point: Point) -> Point:
        """点をそのまま返す."""
        return point

    @override
    def inverse(self) -> Self:
        """逆変換（自身）を返す."""
        return self


@attrs.frozen
class Matrix2d(Transform):
    """2x2 変換行列による XY 平面上の線形変換を表すイミュータブルなクラス.

    列ベクトル ``(x, y)`` に左から掛ける。Point3d の z は変えない。

    Attributes:
        matrix: 2x2 の変換行列（形状が違えば ValueError）
    """

    matrix: npt.NDArray[np.floating] = attrs.field(
        eq=attrs.cmp_using(eq=np.array_equal)
    )

    def __attrs_post_init__(self) -> None:
        if self.matrix.shape != (2, 2):
            msg = f"行列は2x2である必要があります。与えられた形状: {self.matrix.shape}"
            raise ValueError(msg)

    @overload
    def apply(self, point: Point2d) -> Point2d: ...

    @overload
    def apply(self, point: Point3d) -> Point3d: ...

    @override
    def apply(self, point: Point) -> Point:
        """点に行列変換を適用する."""
        v = self.matrix @ np.array([point.x, point.y])
        if isinstance(point, Point2d):
            return Point2d(float(v[0]), float(v[1]))
        return Point3d(float(v[0]), float(v[1]), point.z)

    @override
    def inverse(self) -> Self:
        """逆変換を返す."""
        return self.__class__(np.linalg.inv(self.matrix))


@attrs.frozen
class HeightPlane(Transform):
    """計測した基板表面に 2 次曲面を当てはめ、点の Z に表面高さを足す変換.

    ``points`` は表面上の計測点で、XY と実測 Z を持つ（高さ計測では機械 XY とプローブ Z）。

    曲面 z = c + a*x + b*y + d*x² + e*y² + f*xy を最小二乗で求める。

    x²・y² が反り、xy がねじれを表す。

    ``apply`` は点の XY で曲面を評価し、その値を点の z に足す。

    したがって入力点の z には表面からの相対高さを与える（z=0 なら表面そのもの）。

    計測点の凸包の外も同じ式で外挿する。

    ``points`` が 6 点未満、または 6 点あっても退化（例: 全点が一直線上）していれば ValueError。
    """

    points: tuple[Point3d, ...]
    _c: float = attrs.field(init=False, eq=False)
    _a: float = attrs.field(init=False, eq=False)
    _b: float = attrs.field(init=False, eq=False)
    _d: float = attrs.field(init=False, eq=False)
    _e: float = attrs.field(init=False, eq=False)
    _f: float = attrs.field(init=False, eq=False)

    def __attrs_post_init__(self) -> None:
        if len(self.points) < 6:
            msg = f"pointsは6点以上必要です。与えられた点数: {len(self.points)}"
            raise ValueError(msg)

        design = np.array(
            [[1.0, p.x, p.y, p.x**2, p.y**2, p.x * p.y] for p in self.points]
        )
        if np.linalg.matrix_rank(design, tol=1e-9) < 6:
            msg = (
                "pointsが退化しています。2次曲面フィットには非退化な6点以上が必要です。"
            )
            raise ValueError(msg)

        z = np.array([p.z for p in self.points])
        c, a, b, d, e, f = np.linalg.lstsq(design, z, rcond=None)[0]
        object.__setattr__(self, "_c", float(c))
        object.__setattr__(self, "_a", float(a))
        object.__setattr__(self, "_b", float(b))
        object.__setattr__(self, "_d", float(d))
        object.__setattr__(self, "_e", float(e))
        object.__setattr__(self, "_f", float(f))

    @overload
    def apply(self, point: Point2d) -> Point2d: ...

    @overload
    def apply(self, point: Point3d) -> Point3d: ...

    @override
    def apply(self, point: Point) -> Point:
        """点の XY で曲面を評価し、その高さを z に足した点を返す.

        Point2d は Z を持たないので、そのまま返す。
        """
        if isinstance(point, Point2d):
            return point
        x, y = point.x, point.y
        z_offset = (
            self._c
            + self._a * x
            + self._b * y
            + self._d * x**2
            + self._e * y**2
            + self._f * x * y
        )
        return Point3d(point.x, point.y, point.z + z_offset)

    @override
    def inverse(self) -> Self:
        """表面高さを z から引く逆変換を返す（各点の Z を反転して再フィットする）."""
        return self.__class__(
            points=tuple(Point3d(p.x, p.y, -p.z) for p in self.points)
        )


class Compose(UserList[Transform], Transform):
    """複数の変換を合成するクラス.

    変換は self[0] → self[1] → ... の順に適用する。

    ``Compose([A, B]).apply(p)`` は ``B.apply(A.apply(p))`` と同じ。

    UserList を継承しているため、リストと同様に操作できる。

    Examples:
        >>> compose = Compose([Scale(2.0, 2.0, 1.0), Rotation(90.0)])
    """

    def __init__(self, initlist: Iterable[Transform] | None = None) -> None:
        super().__init__(initlist)

    @overload
    def apply(self, point: Point2d) -> Point2d: ...

    @overload
    def apply(self, point: Point3d) -> Point3d: ...

    @override
    def apply(self, point: Point) -> Point:
        """点に合成変換を適用する."""
        result = point
        for transform in self:
            result = transform.apply(result)
        return result

    @override
    def inverse(self) -> Self:
        """逆変換を返す.

        変換の順序を逆にして、各変換の inverse を適用する。
        例: (A → B → C).inverse() = C^-1 → B^-1 → A^-1

        Returns:
            この変換を打ち消す Compose インスタンス
        """
        return self.__class__([t.inverse() for t in reversed(self)])
