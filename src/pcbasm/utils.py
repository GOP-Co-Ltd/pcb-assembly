import math
import sys
from typing import TypeGuard


def is_finite_number(value: object) -> TypeGuard[int | float]:
    """``bool`` を除く有限な ``int`` / ``float`` かを判定する.

    float に収まらない巨大な int（``math.isfinite`` が ``OverflowError`` を投げる）
    も ``False`` とする。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int) and abs(value) > sys.float_info.max:
        return False
    return math.isfinite(value)
