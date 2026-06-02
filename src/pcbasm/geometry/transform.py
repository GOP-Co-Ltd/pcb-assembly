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
    """3次元空間の座標を表すイミュータブルなクラス.

    Attributes:
        x: X座標
        y: Y座標
        z: Z座標
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
        """Point2d型に変換する（z座標は無視）."""
        return Point2d(x=self.x, y=self.y)

    @classmethod
    def zero(cls, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> Self:
        """指定されていない軸を0で初期化したPoint3dを返す."""
        return cls(x, y, z)


@attrs.frozen
class Point2d:
    """2次元空間の座標を表すイミュータブルなクラス.

    Attributes:
        x: X座標
        y: Y座標
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
        """Point3d型に変換する.

        Args:
            z: Z座標（デフォルト: 0.0）

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
    """3次元スケール変換を表すイミュータブルなクラス.

    Attributes:
        x: X軸方向のスケール係数
        y: Y軸方向のスケール係数
        z: Z軸方向のスケール係数
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
            各軸の逆数をスケール係数とするScaleインスタンス
        """
        return self.__class__(1 / self.x, 1 / self.y, 1 / self.z)

    @classmethod
    def flip(cls, x: bool = False, y: bool = False, z: bool = False) -> Self:
        """指定した軸を反転するスケールを返す.

        Args:
            x: Trueの場合、X軸を反転
            y: Trueの場合、Y軸を反転
            z: Trueの場合、Z軸を反転

        Returns:
            指定軸が反転されたScaleインスタンス
        """
        return cls(
            -1.0 if x else 1.0,
            -1.0 if y else 1.0,
            -1.0 if z else 1.0,
        )


@attrs.frozen
class Rotation(Transform):
    """Z軸周りの回転を表すイミュータブルなクラス.

    Attributes:
        degrees: 回転角度（度数法、反時計回りが正）
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
            反対方向に同じ角度だけ回転するRotationインスタンス
        """
        return self.__class__(-self.degrees)

    @classmethod
    def from_points(cls, base: Point2d, target: Point2d) -> Self:
        """2つのベクトル間の角度からRotationを生成する.

        Args:
            base: 基準ベクトル
            target: 対象ベクトル

        Returns:
            baseからtargetへの回転を表すRotationインスタンス
        """
        dot = base.x * target.x + base.y * target.y
        cross = base.x * target.y - base.y * target.x
        radians = math.atan2(cross, dot)
        return cls(degrees=math.degrees(radians))


@attrs.frozen
class Shift(Transform):
    """平行移動変換を表すイミュータブルなクラス.

    Attributes:
        x: X軸方向の移動量
        y: Y軸方向の移動量
        z: Z軸方向の移動量
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
            反対方向に同じ量だけ移動するShiftインスタンス
        """
        return self.__class__(-self.x, -self.y, -self.z)

    @classmethod
    def from_point(cls, point: Point) -> Self:
        """PointからShiftを生成する."""
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
    """2x2変換行列によるXY平面上の線形変換を表すイミュータブルなクラス.

    Attributes:
        matrix: 2x2の変換行列
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
    """全サンプル点に最小二乗で z = a*x + b*y + c をフィットする高さ補正変換.

    基板表面が平面であるという物理仮定のもとで、計測サンプル点に最小二乗フィッティング を行い、フィットされた平面の式に従って XY
    位置に応じた Z 補正を加算する。 凸包外も同じ平面式で外挿される。
    """

    points: tuple[Point3d, ...]
    _a: float = attrs.field(init=False, eq=False)
    _b: float = attrs.field(init=False, eq=False)
    _c: float = attrs.field(init=False, eq=False)

    def __attrs_post_init__(self) -> None:
        if len(self.points) < 3:
            msg = f"pointsは3点以上必要です。与えられた点数: {len(self.points)}"
            raise ValueError(msg)

        design = np.array([[p.x, p.y, 1.0] for p in self.points])
        if np.linalg.matrix_rank(design[:, :2] - design[0, :2], tol=1e-9) < 2:
            msg = "pointsのXYが同一直線上にあります。3点以上の非共線な点が必要です。"
            raise ValueError(msg)

        z = np.array([p.z for p in self.points])
        a, b, c = np.linalg.lstsq(design, z, rcond=None)[0]
        object.__setattr__(self, "_a", float(a))
        object.__setattr__(self, "_b", float(b))
        object.__setattr__(self, "_c", float(c))

    @overload
    def apply(self, point: Point2d) -> Point2d: ...

    @overload
    def apply(self, point: Point3d) -> Point3d: ...

    @override
    def apply(self, point: Point) -> Point:
        """点にZ高さ補正を適用する.

        Point2dの場合はそのまま返す。Point3dの場合はXYから平面式 z = ax + by + c を評価して z
        に加算する。
        """
        if isinstance(point, Point2d):
            return point
        z_offset = self._a * point.x + self._b * point.y + self._c
        return Point3d(point.x, point.y, point.z + z_offset)

    @override
    def inverse(self) -> Self:
        """逆変換（各点のZ値を反転して再フィット）を返す."""
        return self.__class__(
            points=tuple(Point3d(p.x, p.y, -p.z) for p in self.points)
        )


class Compose(UserList[Transform], Transform):
    """複数の変換を合成するクラス.

    変換は self[0] → self[1] → ... の順に適用される。
    UserListを継承しているため、リストと同様に操作できる。

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

        変換の順序を逆にして、各変換のinverseを適用する。
        例: (A → B → C).inverse() = C^-1 → B^-1 → A^-1

        Returns:
            この変換を打ち消すComposeインスタンス
        """
        return self.__class__([t.inverse() for t in reversed(self)])
