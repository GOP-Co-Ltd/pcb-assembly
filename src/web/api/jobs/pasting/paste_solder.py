"""はんだ塗布ジョブ."""

from __future__ import annotations

from pcbasm import gcode
from pcbasm.pasting.workflow import plan_paste_targets
from pcbasm.pcb import PadHierarchy
from web.api.jobs.board_ops import setup_board
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import JobContext, JobResult
from web.api.jobs.pasting.common import (
    LOADING_DEFAULT_AMOUNT,
    LoadingTotals,
    prepare_paste_workflow,
    resolve_paste_model,
    run_loading_loop,
)


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="paste_solder",
            label="はんだ塗布",
            tab="pasting",
            run=_run_paste_solder,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
                ParamSpec(
                    "amount",
                    "ローディング既定量",
                    "float",
                    LOADING_DEFAULT_AMOUNT,
                    unit="uL",
                ),
                ParamSpec(
                    "interactive_loading", "対話的ローディング", "bool", default=False
                ),
            ),
            requires_pcb=True,
            uses_machine=True,
            notify_on_completion=True,
            accepts_commands=True,
            provides_preview=True,
            loading_param="amount",
        )
    )


def _run_paste_solder(ctx: JobContext) -> JobResult:
    """ボード計測 → 高さ計測 → 銅箔照合 → 補正適用 → ペースト塗布を通しで実行する.

    塗布対象は基板ごとの pad 有効/無効 + 階層 override 設定で絞り込み、各 pad に
    解決済みの塗布設定を適用する。設定ファイル不在時は ``machine.toml`` デフォルトで
    全 pad 有効で動く。
    """
    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera)

        # pad 階層 + 基板ごとの塗布設定（装置不要・前段で解決）
        hierarchy = PadHierarchy.build(result.pcb.components, result.pcb.pads)
        model = resolve_paste_model(ctx, hierarchy)
        targets, error = plan_paste_targets(
            result.pcb,
            hierarchy,
            model,
            initial_purge_ul=result.machine.paste_dispenser.initial_purge_ul,
        )
        if targets is None:
            raise ValueError(error)
        ctx.log(
            f"塗布対象: 有効 {len(targets.routed_pads)} / 全 {len(targets.top_pads)} pads"
            f"（無効 {targets.disabled_count} 件スキップ）"
        )
        purge = targets.initial_purge
        if purge is not None:
            ctx.log(f"初回パージ: {purge.pad_id} に {purge.amount_ul:.3f} uL")

        prepared = prepare_paste_workflow(
            ctx, result, alignment_pads=targets.alignment_pads
        )
        session = prepared.session
        stage = session.stage
        correction = prepared.correction

        total = LoadingTotals()
        with session.make_applicator() as applicator:
            if ctx.params["interactive_loading"]:
                pos = stage.get_position()
                session.klipper.send_gcode(stage.move(x=0, y=0, z=0))
                total = run_loading_loop(ctx, session.klipper, stage, applicator)
                session.klipper.send_gcode(
                    stage.move(x=pos.x, y=pos.y, z=pos.z) + gcode.wait_for_done()
                )

            ctx.progress("リトラクション")
            applicator.retract()

            if purge is not None:
                ctx.progress("初回パージ")
                ctx.checkpoint()
                applicator.deposit_at(
                    purge.pad.center,
                    amount_ul=purge.amount_ul,
                    transform=session.pad_transform(purge.pad, correction),
                )

            # pad を 1 件ずつ apply して per-pad の進捗・設定・abort 境界を確保
            for index, pad in enumerate(targets.routed_pads):
                ctx.progress("塗布", 100.0 * index / len(targets.routed_pads))
                ctx.checkpoint()
                # 対応 Component の無い pad は階層外なので machine 既定で塗る
                params = targets.params_for(pad) or applicator.default_params
                applicator.apply(
                    pad.polygon,
                    params=params,
                    transform=session.pad_transform(pad, correction),
                    line_reference=session.component_positions.get(pad.designator),
                )

    return JobResult(
        summary=(
            f"{prepared.summary()} / "
            f"塗布 有効 {len(targets.routed_pads)} / 全 {len(targets.top_pads)} pads"
            f"（無効 {targets.disabled_count} 件スキップ・"
            f"初回パージ {purge.amount_ul if purge else 0.0:.3f} uL・"
            f"押出合計 {total.amount_ul:+.3f} uL）"
        )
    )
