"""ワイヤ表現と型付きオブジェクトの相互変換.

設定ファイルと成果物のワイヤ表現は、``1`` が ``1.0`` になったり ``True`` が ``1``
として通ったりすると、記録した値と実際に使われた値が食い違う。ここで作る converter は
scalar の型を厳格に照合し、未知キーを拒否する。

構造化の失敗は例外ではなく理由文字列で返す（``memory/feedback_no_try_catch.md``）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import get_origin

import cattrs
from cattrs import Converter


def make_strict_converter() -> Converter:
    """暗黙の型変換と未知キーを拒否する converter を作る."""

    converter = Converter(forbid_extra_keys=True, detailed_validation=True)
    for scalar in (bool, int, float, str):
        converter.register_structure_hook(scalar, _exact_type(scalar))
    # 既定では固定長 tuple が tuple のまま残り、可変長 tuple だけ list になる。
    # JSON 表現を 1 つに揃えるため、どちらも list へ落とす。
    converter.register_unstructure_hook_factory(
        lambda type_: get_origin(type_) is tuple,
        lambda type_, inner: (
            lambda value: [inner.unstructure(item) for item in value]
        ),
    )
    return converter


def structure_strictly[T](
    data: Mapping[str, object],
    target: type[T],
    *,
    converter: Converter,
) -> tuple[T | None, str | None]:
    """``data`` を ``target`` へ構造化し、失敗したら理由を返す."""

    try:
        return converter.structure(data, target), None
    except Exception as error:
        reasons = "; ".join(cattrs.transform_error(error))
        return None, f"{target.__name__} の構造が不正です: {reasons}"


def _exact_type[T](expected: type[T]) -> Callable[[object, type], T]:
    """``bool`` を ``int`` として受け取るような部分型の混入を防ぐ."""

    def structure(value: object, _: type) -> T:
        if type(value) is not expected:
            raise ValueError(f"{expected.__name__} が必要です: {value!r}")
        return value

    return structure


__all__ = [
    "make_strict_converter",
    "structure_strictly",
]
