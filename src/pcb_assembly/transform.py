from typing import Self

import attrs
import numpy as np
import numpy.typing as npt


@attrs.frozen
class Position:
    """3次元空間の位置を表すイミュータブルなクラス.

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
        """NumPy配列からPositionを生成する.

        Args:
            array: 形状が(3,)のfloating配列

        Returns:
            配列の値から生成されたPositionインスタンス

        Raises:
            ValueError: 配列の形状が(3,)でない場合
        """
        if array.shape != (3,):
            raise ValueError(
                f"配列の形状は(3,)である必要がありますが、{array.shape}が渡されました"
            )
        arr = array.astype(np.float64)
        return cls(arr[0], arr[1], arr[2])


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
