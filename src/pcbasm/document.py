"""永続 JSON document の strict な復元.

dataset metadata、塗布量の校正、テスト塗布基板、基板ごとの override が、同じ規則で JSON を attrs の型へ戻す。

- 未知の key と欠落した key を拒否する。書かないことが形式の一部である key だけを :func:`omittable` で宣言する
- 暗黙の型変換をしない（``"1"`` も ``1.0`` も ``True`` も int にしない）。float は有限値だけを受ける
- ブラウザの ``JSON.parse`` と ``JSON.stringify`` を通った document では ``1.0`` が ``1`` になるので、それを読む側は ``int_as_float=True`` で float の欄に int を受ける

欠落を既定値で埋めないのは、既定値を将来変えたときに key を欠いた既存ファイルの意味が気づかれないまま変わるため。
"""

from __future__ import annotations

import math
import types
from collections.abc import Mapping
from typing import Any, Literal, Union, get_args, get_origin

import attrs
import cattrs
from cattrs.gen import make_dict_structure_fn

_OMITTABLE = "pcbasm.document.omittable"


def omittable(default: Any) -> Any:
    """書かないことが形式の一部である key の attrs field を返す（欠落は ``default`` になる）."""
    return attrs.field(default=default, metadata={_OMITTABLE: True})


def structure_document[T](
    data: object, cls: type[T], *, int_as_float: bool = False
) -> T:
    """``data`` を ``cls`` へ strict に復元する.

    Raises:
        Exception: 形が ``cls`` と違う場合（呼び出し側が理由文へ変換する）
    """
    converter = _INT_AS_FLOAT_CONVERTER if int_as_float else _CONVERTER
    return converter.structure(data, cls)


def check_header(
    data: Mapping[str, object], *, kind: str, version: int, label: str
) -> str | None:
    """Document の ``schema_version`` と ``kind`` が期待どおりかを確かめる.

    版を kind より先に見る。版の違う document は kind も違いうるので、版違いと分かる理由を返すため。
    ``label`` は理由文に出す document の呼び名（運転者が読む）。

    Returns:
        一致すれば ``None``、しなければ理由文
    """
    found = data.get("schema_version")
    if type(found) is not int or found != version:
        return f"未対応の{label}です: schema_version={found!r}"
    if data.get("kind") != kind:
        return f"{label}ではありません: kind={data.get('kind')!r}"
    return None


def parse_document[T](
    data: Mapping[str, object],
    cls: type[T],
    *,
    kind: str,
    version: int,
    label: str,
    int_as_float: bool = False,
) -> tuple[T | None, str | None]:
    """:func:`check_header` で版と kind を確かめてから ``cls`` へ strict に復元する.

    版の違う document は移行せずに拒否する。``cls`` は ``kind`` と ``schema_version`` も field に持つ。

    Returns:
        ``(document, None)``。読めなければ ``(None, 理由)``
    """
    error = check_header(data, kind=kind, version=version, label=label)
    if error is not None:
        return None, error
    try:
        return structure_document(data, cls, int_as_float=int_as_float), None
    except Exception as error:
        return None, f"{label} schema v{version} が不正です: {error}"


_TYPE_NAMES: dict[object, str] = {
    float: "有限な数値",
    int: "整数",
    bool: "真偽値",
    str: "文字列",
    type(None): "未設定（null）",
}


def _describe(target: object) -> str:
    """理由文に出す型の呼び名（運転者が読むので Python の型表記を出さない）."""
    origin = get_origin(target)
    if origin is Literal:
        values = get_args(target)
        names = "、".join(str(value) for value in values)
        return f"「{names}」" + ("のいずれか" if len(values) > 1 else "")
    if origin in (Union, types.UnionType):
        return "または".join(_describe(member) for member in get_args(target))
    if origin is tuple:
        return "配列"
    if origin is dict or (isinstance(target, type) and attrs.has(target)):
        return "オブジェクト"
    return _TYPE_NAMES[target]


def _expected(target: object, value: object) -> ValueError:
    return ValueError(f"{_describe(target)}が必要です: {value!r}")


def _make_converter(*, int_as_float: bool) -> cattrs.Converter:
    converter = cattrs.Converter(detailed_validation=False)

    def structure_float(value: object, _: object) -> float:
        if int_as_float and type(value) is int:
            value = float(value)
        if type(value) is not float or not math.isfinite(value):
            raise _expected(float, value)
        return value

    def structure_int(value: object, _: object) -> int:
        if type(value) is not int:
            raise _expected(int, value)
        return value

    def structure_bool(value: object, _: object) -> bool:
        # bool は int のサブクラスなので、hook を分けないと structure_int が受けてしまう
        if type(value) is not bool:
            raise _expected(bool, value)
        return value

    def structure_str(value: object, _: object) -> str:
        if type(value) is not str:
            raise _expected(str, value)
        return value

    def structure_literal(value: object, target: object) -> object:
        if any(
            type(value) is type(expected) and value == expected
            for expected in get_args(target)
        ):
            return value
        raise _expected(target, value)

    def structure_tuple(value: object, target: object) -> tuple[object, ...]:
        if not isinstance(value, (list, tuple)):
            raise _expected(target, value)
        item_types = get_args(target)
        if len(item_types) == 2 and item_types[1] is Ellipsis:
            return tuple(converter.structure(item, item_types[0]) for item in value)
        if len(value) != len(item_types):
            raise ValueError(f"{len(item_types)}要素の配列が必要です: {value!r}")
        return tuple(
            converter.structure(item, item_type)
            for item, item_type in zip(value, item_types, strict=True)
        )

    def structure_union(value: object, target: object) -> object:
        for member in get_args(target):
            if member is type(None):
                if value is None:
                    return None
                continue
            try:
                return converter.structure(value, member)
            except Exception:
                continue
        raise _expected(target, value)

    def structure_dict(value: object, target: object) -> dict[object, object]:
        if not isinstance(value, Mapping):
            raise _expected(target, value)
        key_type, value_type = get_args(target)
        return {
            converter.structure(key, key_type): converter.structure(item, value_type)
            for key, item in value.items()
        }

    def structure_attrs(cls: type) -> Any:
        structure = make_dict_structure_fn(cls, converter)
        names = {field.name for field in attrs.fields(cls)}
        required = tuple(
            field.name
            for field in attrs.fields(cls)
            if not field.metadata.get(_OMITTABLE)
        )

        def hook(value: object, _: object) -> object:
            if not isinstance(value, Mapping):
                raise _expected(cls, value)
            unknown = [str(key) for key in value if key not in names]
            if unknown:
                raise ValueError(f"未知のキーです: {', '.join(unknown)}")
            missing = [name for name in required if name not in value]
            if missing:
                raise ValueError(f"必須の項目がありません: {', '.join(missing)}")
            return structure(value, cls)

        return hook

    converter.register_structure_hook(float, structure_float)
    converter.register_structure_hook(int, structure_int)
    converter.register_structure_hook(bool, structure_bool)
    converter.register_structure_hook(str, structure_str)
    converter.register_structure_hook_factory(attrs.has, structure_attrs)
    converter.register_structure_hook_func(
        lambda target: get_origin(target) is Literal, structure_literal
    )
    converter.register_structure_hook_func(
        lambda target: get_origin(target) is tuple, structure_tuple
    )
    converter.register_structure_hook_func(
        lambda target: get_origin(target) is dict, structure_dict
    )
    converter.register_structure_hook_func(
        lambda target: get_origin(target) in (Union, types.UnionType),
        structure_union,
    )
    return converter


_CONVERTER = _make_converter(int_as_float=False)
_INT_AS_FLOAT_CONVERTER = _make_converter(int_as_float=True)
