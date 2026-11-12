"""ピンセットのロードセルの校正ジョブ（新ファームの機体。既知の重さから counts_per_gram を求める）."""

from __future__ import annotations

from pcbasm.config import MachineConfig
from pcbasm.config.pnp import LoadCellConfig
from pcbasm.hal import MotionError, MotionStage, MotionState
from pcbasm.pnp.load_cell_calibration import (
    COUNTS_PER_GRAM_KEY,
    counts_per_gram,
    describe,
)
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import ApplyPayload, JobResult

# 平均を取る長さ [s]
_MEASURE_SECONDS = 1.0


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="load_cell_calibrate",
            label="ロードセルの校正（counts_per_gram）",
            run=_run_load_cell_calibrate,
            # 荷重を読むだけで XYZ には触れない。終了時の駐機をせず、完了音も鳴らない（servo_setup と同じ）
            uses_machine=False,
        )
    )


def _run_load_cell_calibrate(ctx: JobContext) -> JobResult:
    """風袋引き → 既知の重さを掛けて測る → counts_per_gram を [pnp.motion.load_cell] への保存候補にする.

    押し当てて校正するときは、動かしている間に trigger_force を超えると接触停止で止まるので、止めたまま押すか、
    おもりを載せる。

    ジョブの間は自動の風袋引きを止める。止めないと、今の counts_per_gram が実際より大きい機体では、載せたおもりが
    小さく見えて風袋に取り込まれ、差が 0 に寄る。
    """
    load_cell = _load_cell(ctx.machine)
    stage = ctx.devices.motion_stage()
    if stage.status().contact:
        raise ValueError(
            "接触中は校正できません。Z をピンセットから離す向きに動かしてから始めてください"
        )

    stage.suspend_auto_tare(True)
    try:
        counts, grams, value = _calibrate(ctx, stage)
    finally:
        stage.suspend_auto_tare(False)
    ctx.log(describe(counts, grams, load_cell.counts_per_gram))
    ctx.log(
        "Apply で machine.toml に書いても、開いているステージは今の値のまま。"
        "マシン操作パネルの「再起動（復旧）」の後に効く"
    )

    ctx.progress("完了", 100.0)
    return JobResult(
        summary=f"counts_per_gram = {value:.2f}",
        apply=ApplyPayload(
            label="[pnp.motion.load_cell] の counts_per_gram に書く",
            values={COUNTS_PER_GRAM_KEY: round(value, 2)},
        ),
    )


def _calibrate(ctx: JobContext, stage: MotionStage) -> tuple[float, float, float]:
    """風袋引きから測るまで（風袋からの差 [counts]、載せた重さ [g]、counts_per_gram）."""
    ctx.progress("風袋引き")
    ctx.require(
        "ピンセットに何も載せず、触れずに「風袋引き」を押してください",
        true_label="風袋引き",
    )
    stage.tare()

    ctx.progress("測る")
    while True:
        ctx.require(
            "重さの分かるおもり（10 g 以上）をピンセットに掛けてください（押す向きでも逆向きでも測れます）。"
            "揺れが収まったら「測る」を押してください",
            true_label="測る",
        )
        # 揺れが収まったところで測り、重さはその後に尋ねる（入力の間におもりを触っても測った値は変わらない）
        try:
            counts = stage.measure_load_counts(_MEASURE_SECONDS)
        except MotionError as exc:
            if stage.status().state is MotionState.SHUTDOWN:
                raise  # 再起動すると風袋が変わるので、風袋引きからやり直す
            # 一時的な誤りのサンプル（ファームは読み直して戻る）でも、風袋引きからやり直させない
            ctx.log(f"測れませんでした（{exc}）。載せるところからやり直します")
            continue
        grams = ctx.ask_positive("載せた重さ [g]")
        try:
            value = counts_per_gram(counts, grams)
        except ValueError as exc:
            ctx.log(f"{exc}。載せるところからやり直します")
            continue
        return counts, grams, value


def _load_cell(machine: MachineConfig) -> LoadCellConfig:
    """[pnp.motion.load_cell]（無ければ ValueError）."""
    pnp = machine.pnp
    motion = pnp.motion if pnp is not None else None
    if motion is None or motion.load_cell is None:
        raise ValueError("この機体にはロードセルがありません（[pnp.motion.load_cell]）")
    return motion.load_cell
