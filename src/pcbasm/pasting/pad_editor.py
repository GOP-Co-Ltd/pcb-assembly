"""Pad 編集画面の view-model（表示に要る値と表示文字列）.

backend の API は、ここで組んだ値を契約モデルへ詰めるだけにする。

- 階層ノードごとの実際の塗布方式（auto 判定・フォールバック後）の集計。「塗布方式」列で、選択値から実際に塗る方式が読み取れないノードに方式を添える
- 初回パージと運転時補正の設定と解決結果（設定が不正でも理由を載せて返す）
- 銅箔の表示用の簡略化
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

import attrs
from shapely import Polygon

from pcbasm.config.dispenser import PasteDispenserConfig
from pcbasm.geometry import Point2d
from pcbasm.pasting.fill_path import FillPlan
from pcbasm.pasting.hierarchy import HierKey, PadHierarchy
from pcbasm.pasting.initial_purge import ResolvedInitialPurge
from pcbasm.pasting.labels import flow_calibration_label, purge_selection_label
from pcbasm.pasting.overrides import ResolvedSetting
from pcbasm.pasting.params import PASTE_PARAM_FIELDS
from pcbasm.pasting.paste_volume.runtime_correction import overlapping_crops
from pcbasm.pasting.plan import PastePlan

# 銅箔の表示簡略化の許容誤差 [mm]。基板ビューの 1px 未満に収まる範囲で
# 頂点を間引き、転送量を 1/9 程度へ落とす
COPPER_DISPLAY_TOLERANCE_MM = 0.02

# 方式の値 → 表示ラベル（選択肢の並び順 = 件数の表示順）
_MODE_LABELS: dict[str, str] = {
    choice.value: choice.label
    for field in PASTE_PARAM_FIELDS
    if field.name == "dispense_mode"
    for choice in field.choices
}


@attrs.frozen
class AppliedModeSummary:
    """1 ノード配下の有効 pad が実際に使う塗布方式.

    Attributes:
        counts: 方式ごとの有効 pad 数（選択肢の並び順。0 件の方式は含まない）
        label: 表示文字列（例: ``→ 面``、``→ 点 12 / 線 6``）
        title: ツールチップ用の説明
    """

    counts: dict[str, int]
    label: str
    title: str


@attrs.frozen
class InitialPurgeView:
    """初回パージの設定と解決結果.

    Attributes:
        amount_ul: ``machine.toml`` の初回パージ量 [μL]
        point: 基板設定で明示した点（``None`` は自動）
        default_point: 自動のときに使う、面積最大の有効 pad の中心（順路が空なら ``None``）
        resolved: 実際にパージする点と量（無効・未解決なら ``None``）
        selection_label: 点の指定内容
        error: 解決できない理由
    """

    amount_ul: float
    point: Point2d | None
    default_point: Point2d | None
    resolved: ResolvedInitialPurge | None
    selection_label: str
    error: str | None


def initial_purge_view(plan: PastePlan) -> InitialPurgeView:
    """初回パージの設定と解決結果をまとめる.

    保存済みの座標が基板外などで解決できなくても、理由を載せて返す。

    pad 編集画面ごと開けなくなると、原因の座標を直す手段まで失う。
    """
    point = plan.model.initial_purge_point
    resolution = plan.initial_purge
    return InitialPurgeView(
        amount_ul=plan.config.initial_purge_ul,
        point=point,
        default_point=resolution.default_point,
        resolved=resolution.resolved,
        selection_label=purge_selection_label(point, resolution.default_point),
        error=resolution.error,
    )


@attrs.frozen
class FlowCalibrationView:
    """運転時補正の設定と解決結果.

    ``machine.toml`` 側（校正ファイル・塗布量・crop 寸法）と基板側（測定位置）の両方がそろってはじめて補正を走らせる。
    どちらが欠けているかは ``selection_label`` に出す。

    Attributes:
        enabled: ``machine.toml`` 側が有効か
        points: 基板設定の測定位置（塗る順。補正を走らせない状態でも返す。図から消えると位置を直せない）
        overlapping: 点ごとに、他の点と撮影範囲が重なっているか
        planned: 補正を走らせる状態か
        amount_ul: 1 点あたりの塗布量 [μL]
        crop_size_mm: 測定位置ごとの撮影範囲の一辺 [mm]
        selection_label: 状態（補正しないときはその理由まで）
        error: 設定が不正な理由
    """

    enabled: bool
    points: tuple[Point2d, ...]
    overlapping: tuple[bool, ...]
    planned: bool
    amount_ul: float
    crop_size_mm: float
    selection_label: str
    error: str | None


def flow_calibration_view(plan: PastePlan) -> FlowCalibrationView:
    """運転時補正の設定と解決結果をまとめる（測定位置が基板外・撮影範囲が重なるときも理由を載せて返す）."""
    config = plan.config.volume_correction
    points = plan.model.flow_calibration_points
    planned = plan.flow_calibration
    return FlowCalibrationView(
        enabled=config.enabled,
        points=points,
        overlapping=overlapping_crops(points, crop_size_mm=config.crop_size_mm),
        planned=planned is not None,
        amount_ul=config.amount_ul,
        crop_size_mm=config.crop_size_mm,
        selection_label=flow_calibration_label(
            points,
            planned_amount_ul=None if planned is None else planned.amount_ul,
            error=plan.flow_calibration_error,
        ),
        error=plan.flow_calibration_error,
    )


def display_rings(polygon: Polygon) -> list[list[list[float]]]:
    """銅箔島の頂点を間引いて 3 桁に丸めた環列（exterior が先、以降が穴）を返す.

    各環は閉環（末尾が始点の重複）。簡略化で退化して消えた島は空の環列になる。
    """
    shape = polygon.simplify(COPPER_DISPLAY_TOLERANCE_MM)
    if not isinstance(shape, Polygon) or shape.is_empty:
        return []
    return [
        [[round(x, 3), round(y, 3)] for x, y in ring.coords]
        for ring in (shape.exterior, *shape.interiors)
    ]


def applied_mode_summaries(
    hierarchy: PadHierarchy,
    pad_settings: Mapping[str, ResolvedSetting],
    node_settings: Mapping[HierKey, ResolvedSetting],
    config: PasteDispenserConfig,
) -> dict[HierKey, AppliedModeSummary]:
    """選択値から実際の方式が読み取れないノードについて、配下の有効 pad の方式を集計する.

    返すのは次のどちらかに当たるノードだけ。

    - 解決済みの方式が auto
    - 配下の有効 pad に、解決済みの方式と違う方式で塗る pad がある（フォールバックや子孫の override）

    無効 pad は塗らないので数えない。有効 pad が無いノードは返さない。

    Args:
        hierarchy: 基板の pad 階層
        pad_settings: :func:`~pcbasm.pasting.overrides.resolve_pad_settings` の結果
        node_settings: :func:`~pcbasm.pasting.overrides.resolve_node_settings` の結果
        config: ``MachineConfig.paste_dispenser``（ノズル径と auto 判定の閾値）
    """
    counts: dict[HierKey, Counter[str]] = {}
    for pad in hierarchy.iter_pads():
        setting = pad_settings[pad.ref]
        if not setting.enabled:
            continue
        mode = FillPlan.mode_for_pad(pad.polygon, config=config, params=setting.params)
        for key in hierarchy.node_keys_for_pad(pad):
            counts.setdefault(key, Counter())[mode] += 1

    summaries: dict[HierKey, AppliedModeSummary] = {}
    for key, node_counts in counts.items():
        selected = node_settings[key].params.dispense_mode
        if selected == "auto" or set(node_counts) != {selected}:
            summaries[key] = _summary(node_counts)
    return summaries


def _summary(counts: Counter[str]) -> AppliedModeSummary:
    ordered = {mode: counts[mode] for mode in _MODE_LABELS if mode in counts}
    if len(ordered) == 1:
        label = f"→ {_MODE_LABELS[next(iter(ordered))]}"
    else:
        label = "→ " + " / ".join(
            f"{_MODE_LABELS[mode]} {n}" for mode, n in ordered.items()
        )
    detail = "、".join(f"{_MODE_LABELS[mode]} {n} 件" for mode, n in ordered.items())
    return AppliedModeSummary(
        counts=ordered,
        label=label,
        title=f"実際の塗布方式（有効 pad {sum(ordered.values())} 件）: {detail}",
    )
