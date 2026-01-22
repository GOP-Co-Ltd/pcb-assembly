from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Self, overload

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

    def numpy(self) -> npt.NDArray[np.float64]:
        """位置をNumPy配列として返す.

        Returns:
            [x, y, z]の形式のfloat64配列
        """
        return np.array([self.x, self.y, self.z], dtype=np.float64)

    @classmethod
    def from_numpy(cls, array: npt.NDArray[np.floating]) -> Self:
        """NumPy配列からPoint3dを生成する.

        Args:
            array: 形状が(3,)のfloating配列

        Returns:
            配列の値から生成されたPoint3dインスタンス

        Raises:
            ValueError: 配列の形状が(3,)でない場合
        """
        if array.shape != (3,):
            raise ValueError(
                f"配列の形状は(3,)である必要がありますが、{array.shape}が渡されました"
            )
        arr = array.astype(np.float64)
        return cls(arr[0], arr[1], arr[2])

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


@attrs.frozen
class Scale:
    """3次元スケール変換を表すイミュータブルなクラス.

    Attributes:
        x: X軸方向のスケール係数
        y: Y軸方向のスケール係数
        z: Z軸方向のスケール係数
    """

    x: float = 1.0
    y: float = 1.0
    z: float = 1.0

    def to_matrix(self) -> npt.NDArray[np.float64]:
        """スケール変換を3x3対角行列として返す.

        Returns:
            対角成分が[x, y, z]のfloat64行列
        """
        return np.diag([self.x, self.y, self.z]).astype(np.float64)

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
class Rotation:
    """Z軸周りの回転を表すイミュータブルなクラス.

    Attributes:
        degrees: 回転角度（度数法、反時計回りが正）
    """

    degrees: float = 0.0

    @property
    def radians(self) -> float:
        """回転角度をラジアンで返す."""
        return np.deg2rad(self.degrees)

    def to_matrix(self) -> npt.NDArray[np.float64]:
        """回転変換を3x3行列として返す.

        Returns:
            Z軸周りの回転行列（float64）
        """
        c = np.cos(self.radians)
        s = np.sin(self.radians)
        return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)

    def inverse(self) -> Self:
        """逆回転を返す.

        Returns:
            反対方向に同じ角度だけ回転するRotationインスタンス
        """
        return self.__class__(-self.degrees)


@attrs.frozen
class Transform:
    """スケール、回転、平行移動を組み合わせた変換を表すクラス.

    変換は Scale → Rotation → Translation の順に適用される。

    Attributes:
        scale: スケール変換
        rotation: 回転変換
        translation: 平行移動
    """

    scale: Scale = attrs.Factory(Scale)
    rotation: Rotation = attrs.Factory(Rotation)
    translation: Point3d = attrs.Factory(lambda: Point3d(0.0, 0.0, 0.0))

    def _apply_to_array(
        self, points: npt.NDArray[np.float64]
    ) -> npt.NDArray[np.float64]:
        """NumPy配列に変換を適用する（内部メソッド）.

        Args:
            points: 形状(N, 3)の座標配列

        Returns:
            変換後の座標配列
        """
        # Scale → Rotation → Translation
        # points: (N, 3), matrix: (3, 3) なので転置して計算
        scaled = points @ self.scale.to_matrix().T
        rotated = scaled @ self.rotation.to_matrix().T
        return rotated + self.translation.numpy()

    @overload
    def apply(self, point: Point2d) -> Point2d: ...

    @overload
    def apply(self, point: Point3d) -> Point3d: ...

    def apply(self, point: Point) -> Point:
        """位置に変換を適用する.

        Args:
            point: 変換を適用する位置

        Returns:
            変換後の位置
        """
        if is_2d := isinstance(point, Point2d):
            point = point.to3d()

        points = point.numpy().reshape(1, 3)
        result = self._apply_to_array(points)

        out = Point3d.from_numpy(result[0])
        if is_2d:
            return out.to2d()
        return out

    def inverse(self) -> Self:
        """逆変換を返す.

        Returns:
            この変換を打ち消すTransformインスタンス
        """
        inv_scale = self.scale.inverse()
        inv_rotation = self.rotation.inverse()
        inv_translation_vec = (
            inv_rotation.to_matrix()
            @ inv_scale.to_matrix()
            @ (-self.translation.numpy())
        )
        return self.__class__(
            scale=inv_scale,
            rotation=inv_rotation,
            translation=Point3d.from_numpy(inv_translation_vec),
        )

    def batch(self, points: Iterable[Point3d]) -> list[Point3d]:
        """複数の位置に変換を一括適用する.

        Args:
            points: 変換を適用する位置のイテラブル

        Returns:
            変換後の位置のリスト
        """
        points_list = list(points)
        if not points_list:
            return []

        points = np.array([p.numpy() for p in points_list], dtype=np.float64)
        result = self._apply_to_array(points)
        return [Point3d.from_numpy(row) for row in result]
