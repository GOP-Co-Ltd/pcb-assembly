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
