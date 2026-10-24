"""塗布の前計画（対象 pad・順路・pad ごとの設定・初回パージ・運転時補正）.

装置を動かす前に、基板設定から「何をどの順に、どの設定で塗るか」を決める純関数。
pad 編集画面の preview（順路・塗布経路・初回パージ）と塗布ジョブが同じ :func:`plan_paste` を呼ぶので、画面で見た計画がそのまま実行される。
"""

from __future__ import annotations

from collections.abc import Mapping

import attrs

from pcbasm.board import Layer, Pad
from pcbasm.config.dispenser import PasteDispenserConfig
from pcbasm.geometry import Point2d
from pcbasm.pasting.fill_path import FillPlan
from pcbasm.pasting.initial_purge import (
    InitialPurgeResolution,
    resolve_initial_purge_for,
)
from pcbasm.pasting.overrides import (
    BoardSession,
    PasteSettingsModel,
    ResolvedSetting,
    resolve_pad_settings,
)
from pcbasm.pasting.params import PasteParams
from pcbasm.pasting.paste_volume.runtime_correction import (
    FlowCalibrationPlan,
    plan_flow_calibration,
)
from pcbasm.pasting.route import PasteRouteStop, plan_paste_route


@attrs.frozen
class PastePlan:
    """1 レイヤ分の塗布の前計画.

    設定が不正（初回パージの点や運転時補正の測定点が基板外など）でも計画は返し、理由を :attr:`error` に載せる。
    pad 編集画面は理由を見せて直させ、塗布ジョブは塗り始める前に止まる。

    Attributes:
        config: ``machine.toml`` の ``[paste_dispenser]``
        model: 基板の塗布設定
        resolved: pad ごとの解決済み設定
        pads: 対象レイヤの全 pad（有効/無効問わず、基板の並び）
        route: 有効 pad の塗布順路
        line_references: designator → 部品位置（線塗布の走行方向の基準）
        initial_purge: 初回パージの解決（自動で選ぶ点と、解決できない理由を含む）
        flow_calibration: 運転時補正の計画（無効・不正なら ``None``）
        flow_calibration_error: 運転時補正の設定が不正な理由
    """

    config: PasteDispenserConfig
    model: PasteSettingsModel
    resolved: Mapping[str, ResolvedSetting]
    pads: tuple[Pad, ...]
    route: tuple[PasteRouteStop, ...]
    line_references: Mapping[str, Point2d]
    initial_purge: InitialPurgeResolution
    flow_calibration: FlowCalibrationPlan | None
    flow_calibration_error: str | None

    @property
    def routed_pads(self) -> tuple[Pad, ...]:
        """有効 pad を塗る順に並べたもの."""
        return tuple(stop.pad for stop in self.route)

    @property
    def disabled_count(self) -> int:
        """設定で無効にしたため塗らない pad の数."""
        return len(self.pads) - len(self.route)

    @property
    def error(self) -> str | None:
        """塗り始められない理由（初回パージを先に見る）."""
        return self.initial_purge.error or self.flow_calibration_error

    def params_for(self, pad: Pad) -> PasteParams:
        """Pad の解決済みパラメータを返す."""
        return self.resolved[pad.ref].params

    def line_reference(self, pad: Pad) -> Point2d | None:
        """線塗布の走行方向の基準にする部品位置."""
        return self.line_references.get(pad.designator)

    def fill_plan(self, pad: Pad) -> FillPlan:
        """Pad の塗布経路（塗布ジョブの ``apply`` と同じ入力で組む）."""
        return FillPlan.for_pad(
            pad.polygon,
            config=self.config,
            params=self.params_for(pad),
            line_reference=self.line_reference(pad),
        )


def plan_paste(loaded: BoardSession, *, layer: Layer = Layer.TOP) -> PastePlan:
    """基板設定から ``layer`` の塗布の前計画を組む.

    有効 pad だけを :func:`~pcbasm.pasting.route.plan_paste_route` の順に並べる。
    初回パージと運転時補正は、機能が無効なら解決しない（エラーにもしない）。
    """
    board = loaded.board
    model = loaded.model
    config = loaded.base_config
    resolved = resolve_pad_settings(loaded.hierarchy, model)
    pads = tuple(pad for pad in board.pads if pad.layer is layer)
    route = tuple(plan_paste_route(pad for pad in pads if resolved[pad.ref].enabled))
    flow_calibration, flow_calibration_error = plan_flow_calibration(
        config=config.volume_correction,
        points=model.flow_calibration_points,
        outline=board.outline.polygon,
    )
    return PastePlan(
        config=config,
        model=model,
        resolved=resolved,
        pads=pads,
        route=route,
        line_references={c.designator: c.position for c in board.components},
        initial_purge=resolve_initial_purge_for(
            amount_ul=config.initial_purge_ul,
            point=model.initial_purge_point,
            routed_pads=[stop.pad for stop in route],
            outline=board.outline.polygon,
        ),
        flow_calibration=flow_calibration,
        flow_calibration_error=flow_calibration_error,
    )
