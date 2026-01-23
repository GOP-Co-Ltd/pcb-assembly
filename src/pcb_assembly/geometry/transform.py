from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import UserList
from collections.abc import Iterable
from typing import Self, overload, override

import attrs


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

    def flip(self, x: bool = False, y: bool = False, z: bool = False) -> Self:
        """指定した軸を反転したスケールを返す.

        Args:
            x: Trueの場合、X軸を反転
            y: Trueの場合、Y軸を反転
            z: Trueの場合、Z軸を反転

        Returns:
            指定軸が反転されたScaleインスタンス
        """
        return self.__class__(
            -self.x if x else self.x,
            -self.y if y else self.y,
            -self.z if z else self.z,
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
class Translation(Transform):
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
            反対方向に同じ量だけ移動するTranslationインスタンス
        """
        return self.__class__(-self.x, -self.y, -self.z)


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
