"""塗布パラメータの単一ソース.

pad 単位で上書きできる 8 項目を :class:`PasteParams`（全項目確定）と
:class:`PasteParamsPatch`（差分、``None`` = 継承）の 2 型で表す。項目の一覧・
表示順・UI メタデータは :data:`PASTE_PARAM_FIELDS` が唯一の出典で、
``machine.toml``（:class:`pcbasm.config.PasteDispenser`）からの写しは
``attrs.fields`` で同名フィールドを機械的に行う。

値の制約は ``PasteDispenser.validate_values`` を共用する。
HTTP 境界の :func:`validate_param_values` は上書き可能な項目名も確認し、
ここから先の計算層は検証済みの値を受け取る前提で再検証しない。

装置側の吐出ダイナミクス（レート・加速度・リトラクト・リフト）は pad に依らないので
:class:`DispenseSettings` に分ける。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, Self, cast

import attrs

from pcbasm.config import (
    DispenseMode,
    LineDirection,
    PasteDispenser,
    PasteHeight,
    resolve_paste_height,
)

PasteParamValue = float | str


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
        kind: ``number`` = 数値 / ``choice`` = 選択式 / ``height`` = 数値または ``auto``
        unit: 単位表記（無ければ ``None``）
        choices: ``choice`` の選択肢
    """

    name: str
    label: str
    kind: Literal["number", "choice", "height"]
    unit: str | None = None
    choices: tuple[ChoiceOption, ...] = ()


@attrs.frozen
class PasteParams:
    """Pad 1 枚に適用する塗布パラメータ 8 項目（全項目確定）.

    フィールドの並びは UI の列順と一致させる（:data:`PASTE_PARAM_FIELDS` と同順）。

    Attributes:
        dispense_mode: 塗布方式 auto / dot / line / area
        line_direction: 線塗布の走行方向 unconstrained / outward / inward
        ul_per_mm2: パッド面積あたりのペースト量 [μL/mm²]
        paste_height: 塗布時の基板表面からのノズル高さ [mm]（機械座標の絶対 Z ではない）、または ``auto``（膜厚追従）
        prime_extra_delay: プライム後の追加遅延 [sec]
        bead_width_factor: ビード幅係数（w = nozzle_diameter * bead_width_factor）
        overlap: ジグザグ行間オーバーラップ [0, 1)
        boundary_margin: 外周マージン [mm]
    """

    dispense_mode: DispenseMode
    line_direction: LineDirection
    ul_per_mm2: float
    paste_height: PasteHeight
    prime_extra_delay: float
    bead_width_factor: float
    overlap: float
    boundary_margin: float

    @classmethod
    def from_config(cls, config: PasteDispenser) -> Self:
        """``machine.toml`` の ``[paste_dispenser]`` から同名フィールドを写す."""
        return cls(**{name: getattr(config, name) for name in PASTE_PARAM_NAMES})

    @property
    def paste_height_mm(self) -> float:
        """基板表面からのノズル高さ [mm]（``auto`` は ``ul_per_mm2`` を膜厚として解決）."""
        return resolve_paste_height(self.paste_height, self.ul_per_mm2)

    def patched(self, patch: PasteParamsPatch) -> Self:
        """``patch`` の非 ``None`` 項目で上書きした新しいインスタンスを返す."""
        return attrs.evolve(self, **patch.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """全項目の dict（``**`` 展開で同名キーワード引数へ渡せる）."""
        return attrs.asdict(self)


@attrs.frozen
class PasteParamsPatch:
    """塗布パラメータの差分上書き（``None`` = 継承。JSON では欠落で表現）."""

    dispense_mode: DispenseMode | None = None
    line_direction: LineDirection | None = None
    ul_per_mm2: float | None = None
    paste_height: PasteHeight | None = None
    prime_extra_delay: float | None = None
    bead_width_factor: float | None = None
    overlap: float | None = None
    boundary_margin: float | None = None

    @classmethod
    def from_dict(cls, values: Mapping[str, object]) -> Self:
        """欠落項目を ``None`` として復元する（値は検証済みであること）."""
        kwargs = {name: values.get(name) for name in PASTE_PARAM_NAMES}
        return cls(**cast(dict[str, Any], kwargs))

    def to_dict(self) -> dict[str, PasteParamValue]:
        """非 ``None`` 項目のみの dict."""
        return {
            name: value
            for name in PASTE_PARAM_NAMES
            if (value := getattr(self, name)) is not None
        }

    def updated(
        self,
        values: Mapping[str, PasteParamValue] | None = None,
        *,
        clear: Iterable[str] = (),
    ) -> Self:
        """``values`` で上書きし ``clear`` の項目を継承（``None``）に戻した新パッチ."""
        merged: dict[str, object] = dict(self.to_dict())
        if values:
            merged.update(values)
        for name in clear:
            merged.pop(name, None)
        return self.from_dict(merged)

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in PASTE_PARAM_NAMES)


# UI 列順のフィールド一覧。名前と順序は PasteParams の attrs フィールドと一致する。
PASTE_PARAM_FIELDS: tuple[ParamField, ...] = (
    ParamField(
        "dispense_mode",
        "塗布方式",
        "choice",
        choices=(
            ChoiceOption("auto", "Auto"),
            ChoiceOption("dot", "点"),
            ChoiceOption("line", "線"),
            ChoiceOption("area", "面"),
        ),
    ),
    ParamField(
        "line_direction",
        "線の走行方向",
        "choice",
        choices=(
            ChoiceOption("unconstrained", "指定なし"),
            ChoiceOption("outward", "部品中心 → 外側"),
            ChoiceOption("inward", "外側 → 部品中心"),
        ),
    ),
    ParamField("ul_per_mm2", "面積あたりのペースト量", "number", unit="μL/mm²"),
    ParamField("paste_height", "塗布高さ", "height", unit="mm"),
    ParamField("prime_extra_delay", "プライム後追加遅延", "number", unit="s"),
    ParamField("bead_width_factor", "ビード幅係数", "number"),
    ParamField("overlap", "重なり率", "number"),
    ParamField("boundary_margin", "外周余白", "number", unit="mm"),
)

PASTE_PARAM_NAMES: tuple[str, ...] = tuple(field.name for field in PASTE_PARAM_FIELDS)


def validate_field_names(fields: Iterable[str]) -> str | None:
    """:data:`PASTE_PARAM_NAMES` 外の項目があればエラー文、無ければ ``None``."""
    unknown = [field for field in fields if field not in PASTE_PARAM_NAMES]
    if unknown:
        return f"未知の設定項目です: {', '.join(unknown)}"
    return None


def validate_param_values(values: Mapping[str, object]) -> str | None:
    """HTTP 境界で受けた塗布パラメータ値を検証し、不正なら日本語エラー文を返す."""
    if (message := validate_field_names(values)) is not None:
        return message
    return PasteDispenser.validate_values(values)


@attrs.frozen
class DispenseSettings:
    """装置側の吐出ダイナミクス（pad に依らない）.

    Attributes:
        max_fill_speed: 連続塗布できる移動速度上限 [mm/sec]
        max_dispense_rate: 吐出レート上限 [μL/sec]
        dispense_accel: 吐出加速度 [μL/sec²]
        retract_amount: リトラクション量 [μL]
        retract_rate: リトラクション速度 [μL/sec]
        retract_accel_factor: リトラクション加速度係数（> 1.0）
        lift_height: 塗布後の上昇高さ [mm]
    """

    max_fill_speed: float
    max_dispense_rate: float
    dispense_accel: float
    retract_amount: float
    retract_rate: float
    retract_accel_factor: float
    lift_height: float

    @property
    def retract_accel(self) -> float:
        """リトラクション加速度 a_R [μL/sec²]."""
        return self.retract_accel_factor * self.retract_rate**2 / self.retract_amount

    @classmethod
    def from_config(
        cls, config: PasteDispenser, *, lift_height: float | None = None
    ) -> Self:
        return cls(
            max_fill_speed=config.max_fill_speed,
            max_dispense_rate=config.max_dispense_rate,
            dispense_accel=config.dispense_accel,
            retract_amount=config.retract_amount,
            retract_rate=config.effective_retract_rate,
            retract_accel_factor=config.retract_accel_factor,
            lift_height=config.lift_height if lift_height is None else lift_height,
        )
