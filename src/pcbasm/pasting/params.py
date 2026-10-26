"""塗布パラメータの単一ソース.

pad 単位で上書きできる 10 項目を :class:`PasteParams`（全項目確定）と
:class:`PasteParamsPatch`（差分、欠落 = 継承）の 2 型で表す。項目の一覧・並び・値の制約・表示名は
:class:`PasteParams` の attrs フィールドが唯一の出典で、``machine.toml`` の
``[solder.defaults]`` もこの型で読む。項目名・pad 編集表の列・HTTP 境界の検証は
``attrs.fields`` から導出する。

装置側の吐出ダイナミクス（レート・加速度・リトラクト・リフト）は pad に依らないので
:class:`DispenseMotion` に分ける（設定は ``[solder.dispenser]`` と ``[solder.motion]`` に書く）。

``pcbasm.config`` がこのモジュールの型で ``machine.toml`` を読むので、ここは L0 の単位だけに依存する。
そのため設定ページの metadata は :func:`pcbasm.config.fields.setting` を呼ばずに同じキーの dict で書く（:func:`~pcbasm.config.fields.setting_fields` がそれを ``setting`` に通して検査する）。
``order`` は ``[solder]`` の画面での通し番号。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, Self, get_args, override

import attrs

from pcbasm.validators import (
    as_validator,
    is_finite_number,
    validate_finite_number,
    validate_non_negative_number,
    validate_positive_number,
)

DispenseMode = Literal["auto", "dot", "line", "area", "micro_line"]
DISPENSE_MODES: tuple[DispenseMode, ...] = get_args(DispenseMode)
LineDirection = Literal["unconstrained", "outward", "inward"]
LINE_DIRECTIONS: tuple[LineDirection, ...] = get_args(LineDirection)
PasteHeight = float | Literal["auto"]
# 微小線の終点高。``start`` は始点高の解決結果（数値か auto の計算値）に従う
MicroLineEndHeight = float | Literal["auto", "start"]
PasteParamValue = float | str


_positive = as_validator(validate_positive_number)
_non_negative = as_validator(validate_non_negative_number)
_finite = as_validator(validate_finite_number)


def _height(instance: object, attribute: attrs.Attribute, value: PasteHeight) -> None:
    """正の高さ [mm] か ``auto``."""
    if value != "auto":
        _positive(instance, attribute, value)


def _end_height(
    instance: object, attribute: attrs.Attribute, value: MicroLineEndHeight
) -> None:
    """正の高さ [mm] か ``auto`` か ``start``."""
    if value != "start":
        _height(instance, attribute, value)


def _dispense_mode(_: object, __: attrs.Attribute, value: DispenseMode) -> None:
    if value not in DISPENSE_MODES:
        raise ValueError(f"未知の塗布方式です: {value!r}")


def _line_direction(_: object, __: attrs.Attribute, value: LineDirection) -> None:
    if value not in LINE_DIRECTIONS:
        raise ValueError(f"未知の線走行方向です: {value!r}")


def _overlap(_: object, attribute: attrs.Attribute, value: Any) -> None:
    if not is_finite_number(value) or not 0.0 <= value < 1.0:
        raise ValueError(f"{attribute.name}は0以上1未満である必要があります: {value!r}")


_DISPENSE_MODE_CHOICES = (
    ("auto", "Auto"),
    ("dot", "点"),
    ("line", "線"),
    ("area", "面"),
    ("micro_line", "微小線"),
)
_LINE_DIRECTION_CHOICES = (
    ("unconstrained", "指定なし"),
    ("outward", "部品中心 → 外側"),
    ("inward", "外側 → 部品中心"),
)


@attrs.frozen
class ChoiceOption:
    """選択式パラメータの 1 選択肢（値と表示ラベル）."""

    value: str
    label: str


@attrs.frozen
class ParamField:
    """塗布パラメータ 1 項目の UI メタデータ.

    Attributes:
        name: フィールド名（:class:`PasteParams` の属性名）
        label: 表示ラベル
        kind: ``number`` = 数値 / ``choice`` = 選択式 / ``height`` = 数値または ``auto`` などの値
        unit: 単位表記（無ければ ``None``）
        choices: ``choice`` の選択肢、``height`` の数値以外の値
        column: pad 表でこの項目を描く列の項目名（独立した列なら ``None``）
    """

    name: str
    label: str
    kind: Literal["number", "choice", "height"]
    unit: str | None = None
    choices: tuple[ChoiceOption, ...] = ()
    column: str | None = None


@attrs.frozen(kw_only=True)
class PasteParams:
    """Pad 1 枚に適用する塗布パラメータ 10 項目（全項目確定）.

    フィールド名は基板ごとの override（board_settings）の鍵なので変えない。
    フィールドの並びは UI の列順になる（:data:`PASTE_PARAM_FIELDS`）。

    Attributes:
        dispense_mode: 塗布方式 auto / dot / line / area / micro_line
        line_direction: 線塗布・微小線の走行方向 unconstrained / outward / inward
        ul_per_mm2: パッド面積あたりのペースト量 [μL/mm²]
        paste_height: 塗布時の基板表面からのノズル高さ [mm]（機械座標の絶対 Z ではない）、または ``auto``（膜厚追従）。微小線では使わない
        prime_extra_delay: プライム後の追加遅延 [sec]
        bead_width_factor: ビード幅係数（w = nozzle_diameter * bead_width_factor）
        overlap: ジグザグ行間オーバーラップ [0, 1)
        boundary_margin: 外周マージン [mm]（負なら pad 外形をはみ出して塗る）
        micro_line_z_start: 微小線の始点で基板表面から取る隙間 [mm]、または ``auto``（塗布量から計算）
        micro_line_z_end: 微小線の終点で基板表面から取る隙間 [mm]、``auto``（塗布量から計算）、または ``start``（始点高に従う）
    """

    dispense_mode: DispenseMode = attrs.field(
        default="auto",
        validator=_dispense_mode,
        metadata={
            "label": "塗布方式",
            "group": "塗布方式",
            "order": 50,
            "choices": _DISPENSE_MODE_CHOICES,
        },
    )
    line_direction: LineDirection = attrs.field(
        default="unconstrained",
        validator=_line_direction,
        metadata={
            "label": "線の走行方向",
            "group": "塗布方式",
            "order": 51,
            "choices": _LINE_DIRECTION_CHOICES,
        },
    )
    ul_per_mm2: float = attrs.field(
        validator=_positive,
        metadata={
            "label": "面積あたりのペースト量",
            "unit": "uL/mm²",
            "group": "吐出の基本",
            "order": 14,
        },
    )
    paste_height: PasteHeight = attrs.field(
        validator=_height,
        metadata={"label": "塗布高さ", "unit": "mm", "group": "Z 高さ", "order": 80},
    )
    prime_extra_delay: float = attrs.field(
        default=0.0,
        validator=_non_negative,
        metadata={
            "label": "プライム後追加遅延",
            "unit": "s",
            "group": "プライム・パージ",
            "order": 40,
            "minimum": 0.0,
        },
    )
    bead_width_factor: float = attrs.field(
        default=1.0,
        validator=_positive,
        metadata={"label": "ビード幅係数", "group": "塗布経路", "order": 60},
    )
    overlap: float = attrs.field(
        default=0.0,
        validator=_overlap,
        metadata={
            "label": "重なり率",
            "group": "塗布経路",
            "order": 61,
            "minimum": 0.0,
        },
    )
    boundary_margin: float = attrs.field(
        default=0.0,
        validator=_finite,
        metadata={"label": "外周余白", "unit": "mm", "group": "塗布経路", "order": 62},
    )
    micro_line_z_start: PasteHeight = attrs.field(
        default="auto",
        validator=_height,
        metadata={"label": "始点高", "unit": "mm", "group": "微小線", "order": 70},
    )
    micro_line_z_end: MicroLineEndHeight = attrs.field(
        default="start",
        validator=_end_height,
        metadata={"label": "終点高", "unit": "mm", "group": "微小線", "order": 71},
    )

    @property
    def height_fields(self) -> tuple[str, ...]:
        """塗布高さに使う項目名（微小線は始点高・終点高、それ以外は ``paste_height``）."""
        if self.dispense_mode == "micro_line":
            return ("micro_line_z_start", "micro_line_z_end")
        return ("paste_height",)

    def patched(self, patch: PasteParamsPatch) -> Self:
        """``patch`` の項目で上書きした新しいインスタンスを返す."""
        return attrs.evolve(self, **patch.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """全項目の dict（``**`` 展開で同名キーワード引数へ渡せる）."""
        return attrs.asdict(self)


_PASTE_PARAM_ATTRIBUTES: dict[str, attrs.Attribute] = attrs.fields_dict(PasteParams)
PASTE_PARAM_NAMES: tuple[str, ...] = tuple(_PASTE_PARAM_ATTRIBUTES)


def _validate_field_values(values: Mapping[str, object]) -> str | None:
    """渡された項目の値だけを、その attrs フィールドの validator で検証する."""
    for name, value in values.items():
        attribute = _PASTE_PARAM_ATTRIBUTES[name]
        try:
            attribute.validator(None, attribute, value)  # type: ignore[misc]
        except ValueError as exc:
            return str(exc)
    return None


def validate_field_names(fields: Iterable[str]) -> str | None:
    """:data:`PASTE_PARAM_NAMES` 外の項目があればエラー文、無ければ ``None``."""
    unknown = [field for field in fields if field not in _PASTE_PARAM_ATTRIBUTES]
    if unknown:
        return f"未知の設定項目です: {', '.join(unknown)}"
    return None


def validate_param_values(values: Mapping[str, object]) -> str | None:
    """HTTP 境界で受けた塗布パラメータ値を検証し、不正なら日本語エラー文を返す."""
    if (message := validate_field_names(values)) is not None:
        return message
    return _validate_field_values(values)


class PasteParamsPatch:
    """塗布パラメータの差分上書き（書かれていない項目 = 継承。JSON でも欠落で表現）.

    項目名は :data:`PASTE_PARAM_NAMES` だけを受け付ける。値は検証済みであること。
    """

    __slots__ = ("_values",)

    def __init__(self, **values: PasteParamValue | None) -> None:
        if (message := validate_field_names(values)) is not None:
            raise TypeError(message)
        self._values = {
            name: value
            for name in PASTE_PARAM_NAMES
            if (value := values.get(name)) is not None
        }

    def to_dict(self) -> dict[str, PasteParamValue]:
        """書かれている項目だけの dict（:data:`PASTE_PARAM_NAMES` の順）."""
        return dict(self._values)

    def updated(
        self,
        values: Mapping[str, PasteParamValue] | None = None,
        *,
        clear: Iterable[str] = (),
    ) -> Self:
        """``values`` で上書きし ``clear`` の項目を継承に戻した新パッチ."""
        merged: dict[str, PasteParamValue | None] = {**self._values, **(values or {})}
        for name in clear:
            merged.pop(name, None)
        return type(self)(**merged)

    @property
    def is_empty(self) -> bool:
        return not self._values

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PasteParamsPatch):
            return NotImplemented
        return self._values == other._values

    @override
    def __hash__(self) -> int:
        return hash(tuple(self._values.items()))

    @override
    def __repr__(self) -> str:
        items = ", ".join(f"{name}={value!r}" for name, value in self._values.items())
        return f"PasteParamsPatch({items})"


# pad 表で独立した列にせず、ほかの項目の列に描く項目 → その列の項目名
_COLUMNS: dict[str, str] = {
    "micro_line_z_start": "paste_height",
    "micro_line_z_end": "paste_height",
}


# 高さの項目（validator で見分ける）→ 数値の代わりに選べる値と表示名
_HEIGHT_KEYWORDS: dict[object, tuple[tuple[str, str], ...]] = {
    _height: (("auto", "Auto"),),
    _end_height: (("start", "始点に従う"), ("auto", "Auto")),
}


def _param_field(attribute: attrs.Attribute) -> ParamField:
    # 数値か auto などを受け付ける高さの項目は height の入力欄で描き、数値以外の値を choices に載せる
    keywords = _HEIGHT_KEYWORDS.get(attribute.validator)
    choices = tuple(
        ChoiceOption(value, label)
        for value, label in keywords or attribute.metadata.get("choices", ())
    )
    return ParamField(
        attribute.name,
        attribute.metadata["label"],
        "height" if keywords else "choice" if choices else "number",
        unit=attribute.metadata.get("unit"),
        choices=choices,
        column=_COLUMNS.get(attribute.name),
    )


# UI 列順のフィールド一覧（PasteParams の attrs フィールドの順）
PASTE_PARAM_FIELDS: tuple[ParamField, ...] = tuple(
    _param_field(attribute) for attribute in _PASTE_PARAM_ATTRIBUTES.values()
)


@attrs.frozen(kw_only=True)
class DispenseMotion:
    """吐出の動き（pad に依らない装置側の値。fill sequence が使う）.

    :attr:`pcbasm.config.solder.SolderConfig.dispense_motion` が ``[solder.dispenser]`` と ``[solder.motion]`` から組み立てる。
    値の範囲は設定の読み込みが検証する。

    Attributes:
        max_fill_speed: 連続塗布できる移動速度上限 [mm/sec]
        max_dispense_rate: 吐出レート上限 [μL/sec]
        dispense_accel: 吐出加速度 [μL/sec²]
        retract_amount: リトラクション量 [μL]
        retract_rate: リトラクション速度 [μL/sec]
        retract_accel_factor: リトラクション加速度係数（> 1.0）
        lift_height: 塗布後の上昇高さ [mm]
        micro_line_settle_seconds: 微小線で上空に押し出してから下降するまでの静定待ち [sec]
        post_dispense_settle_seconds: 塗布の移動が終わってからヘッドを上げるまでの静定待ち [sec]
    """

    max_fill_speed: float
    max_dispense_rate: float
    dispense_accel: float
    retract_amount: float
    retract_rate: float
    retract_accel_factor: float
    lift_height: float
    micro_line_settle_seconds: float
    post_dispense_settle_seconds: float

    @property
    def retract_accel(self) -> float:
        """リトラクション加速度 a_R [μL/sec²]."""
        return self.retract_accel_factor * self.retract_rate**2 / self.retract_amount
