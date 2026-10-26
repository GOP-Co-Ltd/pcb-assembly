"""はんだ塗布ジョブ."""

from __future__ import annotations

from functools import partial

from pcbasm.gcode import GCode
from pcbasm.pasting.nozzle_clean import resolve_nozzle_clean
from pcbasm.pasting.overrides import BoardSession
from pcbasm.pasting.paste_volume.runtime_correction import run_flow_calibration
from pcbasm.pasting.plan import plan_paste
from pcbasm.pasting.prepare import prepare_paste
from pcbasm.pasting.purge_check import purge_with_cleaning
from web.api.jobs.pasting import PasteStores
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult
from web.api.jobs.steps import (
    LOADING_DEFAULT_AMOUNT,
    LoadingPanel,
    LoadingTotals,
    align_events,
    loading_loop,
    setup_paste_session,
)


def register(catalog: JobCatalog, stores: PasteStores) -> None:
    catalog.register(
        JobDefinition(
            name="paste_solder",
            label="はんだ塗布",
            run=partial(_run_paste_solder, stores=stores),
            params=(
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
            loading=LoadingPanel(amount="amount"),
        )
    )


def _run_paste_solder(ctx: JobContext, *, stores: PasteStores) -> JobResult:
    """ボード計測 → 高さ計測 → 銅箔照合 → 補正適用 → ペースト塗布を通しで実行する.

    塗布対象は基板ごとの pad 有効/無効 + 階層 override 設定で絞り込み、各 pad に
    解決済みの塗布設定を適用する。設定ファイル不在時は ``machine.toml`` デフォルトで
    全 pad 有効で動く。
    """
    with ctx.open_camera() as camera:
        session = setup_paste_session(ctx, camera)
        stage = session.stage

        # 位置合わせに数分かかるので、クリーニング位置の教示ミスはここで顕在化させる
        nozzle_clean = resolve_nozzle_clean(ctx.machine, stage, log=ctx.log)

        # pad 階層 + 基板ごとの塗布設定（装置不要・前段で解決）
        assert ctx.source_pcb is not None  # requires_pcb=True
        plan = plan_paste(
            BoardSession.load(
                stores.overrides,
                ctx.source_pcb,
                session.board_frame.board,
                ctx.machine.solder,
            )
        )
        if plan.error is not None:
            raise ValueError(plan.error)
        ctx.log(
            f"塗布対象: 有効 {len(plan.route)} / 全 {len(plan.pads)} pads"
            f"（無効 {plan.disabled_count} 件スキップ）"
        )
        purge = plan.initial_purge.resolved
        if purge is not None:
            ctx.log(f"初回パージ: {purge.label} に {purge.amount_ul:.3f} uL")

        ctx.progress("高さ計測")
        correction = prepare_paste(
            session,
            plan.routed_pads,
            on_event=align_events(ctx),
            frame_sink=ctx.frame,
        )
        ctx.log(correction.alignment.summary())

        total = LoadingTotals()
        with session.make_applicator() as applicator:
            if ctx.params["interactive_loading"]:
                pos = stage.get_position()
                session.klipper.send_gcode(stage.move(x=0, y=0, z=0))
                total = loading_loop(ctx, session.klipper, stage, applicator)
                session.klipper.send_gcode(
                    stage.move(x=pos.x, y=pos.y, z=pos.z) + GCode.wait_for_done()
                )

            ctx.progress("リトラクション")
            applicator.retract()

            if purge is not None:
                ctx.progress("初回パージ")
                # 詰まりはここで顕在化させる。パージが写らないまま pad を塗り始めると
                # 基板を 1 枚無駄にするので、掃除してやり直してから先へ進む。
                purge_with_cleaning(
                    ctx,
                    session,
                    correction,
                    applicator,
                    purge,
                    nozzle_clean=nozzle_clean,
                )

            flow = plan.flow_calibration
            outcome = (
                None
                if flow is None
                else run_flow_calibration(
                    ctx,
                    session,
                    correction,
                    applicator,
                    flow,
                    calibration_dir=stores.paste_volume_calibration_dir,
                )
            )
            if outcome is not None:
                applicator.adopt_rotations_per_ul(outcome.rotations_per_ul)

            # pad を 1 件ずつ apply して per-pad の進捗・設定・abort 境界を確保
            for index, pad in enumerate(plan.routed_pads):
                ctx.progress("塗布", 100.0 * index / len(plan.route))
                ctx.checkpoint()
                applicator.apply(
                    pad.polygon,
                    params=plan.params_for(pad),
                    transform=session.pad_transform(pad, correction),
                    line_reference=plan.line_reference(pad),
                )

    return JobResult(
        summary=(
            f"{correction.alignment.summary()} / "
            f"塗布 有効 {len(plan.route)} / 全 {len(plan.pads)} pads"
            f"（無効 {plan.disabled_count} 件スキップ・"
            f"初回パージ {purge.amount_ul if purge else 0.0:.3f} uL・"
            f"押出合計 {total.amount_ul:+.3f} uL）"
            + ("" if outcome is None else f" / {outcome.summary()}")
        )
    )
