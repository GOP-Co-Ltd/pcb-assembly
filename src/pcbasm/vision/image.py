"""画像型定義."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import cv2
import numpy as np
import numpy.typing as npt

type ImageArray = npt.NDArray[Any]
type ImageSize = tuple[int, int]


def safe_move_distance(roi_size: tuple[float, float], margin: float = 0.2) -> float:
    """関心領域から出ない安全な移動距離を計算する.

    Args:
        roi_size: 関心領域のサイズ (width, height)
        margin: 安全マージン（デフォルト: 0.2）

    Returns:
        片方向の安全な移動距離
    """
    return min(roi_size) * (1.0 - margin) / 2


class Image:
    """3チャネルカラー画像 (BGR) を保持するイミュータブルなクラス."""

    def __init__(self, data: ImageArray) -> None:
        """カラー画像を初期化.

        Args:
            data: 入力画像（任意のチャネル数、dtypeを受け付ける）
        """
        match data.ndim:
            case 2:
                converted = cv2.cvtColor(data, cv2.COLOR_GRAY2BGR)
            case 3 if data.shape[2] == 4:
                converted = cv2.cvtColor(data, cv2.COLOR_BGRA2BGR)
            case 3 if data.shape[2] == 3:
                converted = data
            case _:
                raise ValueError(f"不正な画像形状: {data.shape}")

        self._data = converted.astype(np.uint8)

    @property
    def width(self) -> int:
        """画像の幅."""
        return int(self._data.shape[1])

    @property
    def height(self) -> int:
        """画像の高さ."""
        return int(self._data.shape[0])

    @property
    def size(self) -> ImageSize:
        """画像のサイズ (width, height)."""
        return (self.width, self.height)

    def numpy(self) -> ImageArray:
        """内部配列を返す."""
        return self._data

    def copy(self) -> Self:
        """画像のコピーを返す."""
        return self.__class__(self._data.copy())

    def save(self, path: Path) -> None:
        """画像をファイルに保存."""
        cv2.imwrite(str(path), self._data)

    def crop_center(self, size: ImageSize) -> Self:
        """画像の中心から指定サイズで切り出す.

        Args:
            size: 切り出すサイズ (width, height)

        Returns:
            切り出した画像
        """
        crop_w, crop_h = size
        center_x, center_y = self.width // 2, self.height // 2
        x1 = center_x - crop_w // 2
        y1 = center_y - crop_h // 2
        x2 = x1 + crop_w
        y2 = y1 + crop_h
        return self.__class__(self._data[y1:y2, x1:x2])

    @classmethod
    def load(cls, path: Path) -> Self:
        """ファイルから画像を読み込み."""
        data = cv2.imread(str(path))
        if data is None:
            raise FileNotFoundError(f"画像を読み込めません: {path}")
        return cls(data)
