"""設定ページの項目を、machine.toml のスキーマに付けた attrs の metadata から作る.

設定ページに出す項目は、各節のスキーマの attrs フィールドに :func:`setting` の metadata を付ける（``attrs.field(metadata=setting(...))``）。
:class:`~pcbasm.config.MachineConfig` の最上位のフィールドには :func:`section` の metadata で画面の見出しと並び順を付ける。
:func:`setting_fields` がそれを辿り、設定ページ・書き込みの受け付け・書込キーの検査が使う項目の一覧を作る。

metadata の意味は次のとおり。

- ``label``: 表示名（設定ページと pad 編集表で共用する）
- ``unit``: 単位
- ``group``: 設定ページのカードの見出し（機能ページのフォームもカード単位で項目を選ぶ）
- ``order``: 節の中での並び順（小さいほど上。カードは最初の項目の位置に並ぶ）
- ``choices``: 選択式の ``(値, 表示名)`` の組
- ``minimum``: 入力欄の下限（validator が受け付ける最小値。0 を受け付けない項目には付けない）
- ``clearable``: 空欄を「無効にする」として保存する
- ``set``: TOML では別々のキーに書き、1 組で読み書きする座標（``("x", "y", "z")``）

:mod:`pcbasm.pasting.params` は config から import できない（config がそれを import する）ので、同じキーの metadata を dict で書く。
:func:`setting_fields` はどの metadata も :func:`setting` に通すので、キーの書き損じや抜けはそこで TypeError になる。

値の範囲は各フィールドの attrs の validator が持つ（``__attrs_post_init__`` はほかの項目との関係だけを見る）。
:meth:`SettingField.validate` はその validator だけで 1 項目を検証し、設定エラーで全体の検証が誤りを隠す間も、書き込む値を項目ごとに確かめられるようにする。
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args, get_origin, get_type_hints

import attrs

from pcbasm.pasting.params import PasteHeight

if TYPE_CHECKING:
    from pcbasm.config import MachineConfig

type SettingKind = Literal[
    "float", "int", "str", "float_or_auto", "float_pair", "float_triple", "choice"
]

XYZ = ("x", "y", "z")

# フィールドの型 → 入力の種類（選択式と座標の組は _kind が決める）
_KINDS: dict[object, SettingKind] = {
    float: "float",
    float | None: "float",
    int: "int",
    str: "str",
    Path: "str",
    PasteHeight: "float_or_auto",
    tuple[float, float]: "float_pair",
}


def setting(
    *,
    label: str,
    group: str,
    order: int,
    unit: str | None = None,
    choices: tuple[tuple[str, str], ...] = (),
    minimum: float | None = None,
    clearable: bool = False,
    set: tuple[str, ...] = (),
) -> dict[str, Any]:
    """設定ページに出す項目の attrs の metadata を作る."""
    return {
        "label": label,
        "group": group,
        "order": order,
        "unit": unit,
        "choices": choices,
        "minimum": minimum,
        "clearable": clearable,
        "set": set,
    }


def section(*, label: str, order: int) -> dict[str, Any]:
    """設定ページの 1 画面になる最上位の節の attrs の metadata を作る."""
    return {"label": label, "order": order}


@attrs.frozen
class SettingField:
    """設定ページの 1 項目.

    Attributes:
        key: 設定キー（属性のパス。``set`` の無い項目では TOML のパスと同じ）
        section: 最上位の節の名前（設定ページの 1 画面）
        section_label: 節の見出し
        group: カードの見出し
        label: 表示名
        kind: 入力の種類（フィールドの型から決まる）
        unit: 単位
        choices: 選択式の ``(値, 表示名)`` の組
        minimum: 入力欄の下限
        clearable: 空欄を「無効にする」として保存する
        set: 1 組で書く TOML のキー（座標の ``x`` / ``y`` / ``z``。それ以外は空）
        attribute: 項目の attrs フィールド（値の検証に使う）
    """

    key: str
    section: str
    section_label: str
    group: str
    label: str
    kind: SettingKind
    unit: str | None
    choices: tuple[tuple[str, str], ...]
    minimum: float | None
    clearable: bool
    set: tuple[str, ...]
    _attribute: attrs.Attribute = attrs.field(alias="attribute", eq=False, repr=False)

    def validate(self, value: object) -> None:
        """値をこの項目の validator だけで検証する（ほかの項目との関係は全体の検証が見る）.

        Raises:
            ValueError: 値が範囲に入らない場合
        """
        if self._attribute.validator is not None:
            self._attribute.validator(None, self._attribute, value)

    def resolve(self, machine: MachineConfig) -> object:
        """既定値まで解決した実効値を返す（任意項目が未設定か、機能の節が無ければ None）."""
        value: object = machine
        for name in self.key.split("."):
            if value is None:
                return None
            value = getattr(value, name)
        return value


@functools.cache
def setting_fields() -> tuple[SettingField, ...]:
    """設定ページに出す全項目を、節の並び順、節の中の並び順で返す.

    Raises:
        ValueError: 1 つの節の中で ``order`` が重なる場合（params と config で通し番号を分け合うため）
    """
    # MachineConfig を組む pcbasm.config がこのモジュールを import するので、ここで import する
    from pcbasm.config import MachineConfig

    hints = get_type_hints(MachineConfig)
    ordered: dict[tuple[int, int], SettingField] = {}
    for top in attrs.fields(MachineConfig):
        if not top.metadata:
            continue
        meta = section(**top.metadata)
        # 機能の節（solder など）は無ければ None なので、None でない側の型を辿る
        (cls,) = (
            hint
            for hint in get_args(hints[top.name]) or (hints[top.name],)
            if attrs.has(hint)
        )
        for order, field in _leaves(cls, top.name, (top.name, meta["label"])):
            position = (meta["order"], order)
            if position in ordered:
                raise ValueError(
                    f"{ordered[position].key} と {field.key} の order が同じです: {order}"
                )
            ordered[position] = field
    return tuple(ordered[position] for position in sorted(ordered))


def _leaves(
    cls: type, prefix: str, top: tuple[str, str]
) -> Iterator[tuple[int, SettingField]]:
    """``cls`` の metadata の付いたフィールドを、下位表の中まで辿って返す."""
    hints = get_type_hints(cls)
    for attribute in attrs.fields(cls):
        key = f"{prefix}.{attribute.name}"
        hint = hints[attribute.name]
        if not attribute.metadata:
            if attrs.has(hint):
                yield from _leaves(hint, key, top)
            continue
        meta = setting(**attribute.metadata)
        yield (
            meta["order"],
            SettingField(
                key=key,
                section=top[0],
                section_label=top[1],
                group=meta["group"],
                label=meta["label"],
                kind=_kind(hint, meta["set"]),
                unit=meta["unit"],
                choices=meta["choices"],
                minimum=meta["minimum"],
                clearable=meta["clearable"],
                set=meta["set"],
                attribute=attribute,
            ),
        )


def _kind(hint: object, set: tuple[str, ...]) -> SettingKind:
    if set:
        return "float_triple"
    if get_origin(hint) is Literal:
        return "choice"
    return _KINDS[hint]
