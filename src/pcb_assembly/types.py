import attrs
import numpy as np


@attrs.define(slots=True, frozen=True)
class Position:
    """位置情報を保持するクラス."""

    x: float
    y: float
    z: float


type HWC = tuple[int, int, int]  # Height, Width, Channels
type Image = np.ndarray[HWC, np.dtype[np.uint8]]
