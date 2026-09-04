"""Attrs クラスから pydantic リクエスト/レスポンスモデルを機械的に生成する.

router がドメインの attrs 値オブジェクトを 1 対 1 で写した pydantic モデルを手書きすると、
フィールド追加のたびに 2 箇所を同期する必要が生じる。:func:`mirror_model` は attrs の
フィールド定義（名前・型・スカラー既定値）から同形の pydantic モデルを生成し、
JSON 形状（キー名・型）を attrs 側の単一定義に従わせる。

変換規則:

- 入れ子の attrs クラスは再帰的に mirror する（同じクラスは同じモデルに解決）
- ``tuple[X, ...]`` は ``list[X']``（JSON 配列）
- ``X | None`` / ``Literal`` / ``str`` / ``float`` / ``int`` / ``bool`` はそのまま
- スカラーの既定値は保持し、``attrs.Factory`` 既定は必須項目にする（リクエストの契約を
  緩めない）
"""

from __future__ import annotations

import operator
import types
from functools import reduce
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

import attrs
from pydantic import BaseModel, ConfigDict, create_model

_MIRROR_CONFIG = ConfigDict(extra="forbid", strict=True, from_attributes=True)
_SCALARS: tuple[type, ...] = (str, float, int, bool, type(None))
_cache: dict[type, type[BaseModel]] = {}


def mirror_model(cls: type, *, name: str | None = None) -> type[BaseModel]:
    """Attrs クラス ``cls`` と同形の pydantic モデルを返す（同一クラスはキャッシュ）.

    生成モデルは ``extra="forbid"`` / ``strict=True`` / ``from_attributes=True``。
    attrs インスタンスからレスポンスを組むときは ``model_validate(obj, strict=False)``
    （tuple → list の変換を許す）を使う。

    Args:
        cls: ``attrs.frozen`` 等で定義された attrs クラス
        name: 生成するモデル名（既定は ``f"{cls.__name__}Model"``）

    Raises:
        TypeError: attrs クラスでない、または対応外の型注釈を含む場合
    """
    if not attrs.has(cls):
        raise TypeError(f"attrs クラスではありません: {cls!r}")
    cached = _cache.get(cls)
    if cached is not None:
        return cached
    hints = get_type_hints(cls)
    fields: dict[str, Any] = {}
    for field in attrs.fields(cls):
        annotation = _mirror_type(hints[field.name], owner=cls, field=field.name)
        fields[field.name] = (annotation, _default_for(field))
    model = create_model(
        name or f"{cls.__name__}Model", __config__=_MIRROR_CONFIG, **fields
    )
    _cache[cls] = model
    return model


def _default_for(field: attrs.Attribute[Any]) -> Any:
    default: Any = field.default
    # attrs.Factory インスタンスは `.factory` を持つ（型スタブ上は関数扱いのため属性で判定）
    if default is attrs.NOTHING or hasattr(default, "factory"):
        return ...
    return default


def _mirror_type(annotation: Any, *, owner: type, field: str) -> Any:
    origin = get_origin(annotation)
    if origin is None:
        if annotation in _SCALARS:
            return annotation
        if isinstance(annotation, type) and attrs.has(annotation):
            return mirror_model(annotation)
        raise TypeError(f"{owner.__name__}.{field}: 対応外の型です: {annotation!r}")
    if origin is Literal:
        return annotation
    if origin is tuple:
        args = get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            return list[_mirror_type(args[0], owner=owner, field=field)]
        raise TypeError(
            f"{owner.__name__}.{field}: 可変長 tuple のみ対応です: {annotation!r}"
        )
    if origin is Union or origin is types.UnionType:
        return reduce(
            operator.or_,
            (
                _mirror_type(arg, owner=owner, field=field)
                for arg in get_args(annotation)
            ),
        )
    raise TypeError(f"{owner.__name__}.{field}: 対応外の型です: {annotation!r}")
