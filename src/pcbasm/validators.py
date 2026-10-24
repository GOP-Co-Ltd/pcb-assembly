"""値の検証に共通する判定（標準ライブラリだけに依存する）.

``validate_*`` は不正なら理由の文字列を、正しければ ``None`` を返す。
"""

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


def validate_finite_number(name: str, value: float) -> str | None:
    """有限値であるべき値を検証する."""
    if not is_finite_number(value):
        return f"{name}は有限な数値である必要があります: {value!r}"
    return None


def validate_positive_number(name: str, value: float) -> str | None:
    """正の有限値であるべき値を検証する."""
    if not is_finite_number(value) or value <= 0:
        return f"{name}は正の有限値である必要があります: {value!r}"
    return None


def validate_non_negative_number(name: str, value: float) -> str | None:
    """0 以上の有限値であるべき値を検証する."""
    if not is_finite_number(value) or value < 0:
        return f"{name}は0以上の有限値である必要があります: {value!r}"
    return None


def validate_positive_int(name: str, value: object) -> str | None:
    """1 以上の整数であるべき値を検証する."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return f"{name}は1以上の整数である必要があります: {value!r}"
    return None


def validate_positive_odd_integer(name: str, value: object) -> str | None:
    """正の奇数であるべき値を検証する."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value % 2 == 0
    ):
        return f"{name}は正の奇数である必要があります: {value!r}"
    return None
