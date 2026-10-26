"""塗布量校正の再フィットジョブ（収集済み session から、検出ハイパラを変えて校正を作り直す。装置不要）.

校正そのものは収集ジョブ（`paste_volume_calibration`）が最後に作る。こちらは
**検出ハイパラを変えて作り直す**ための経路で、装置も 1 時間の収集もペーストも
要らずに数秒で終わる。実行そのものがハイパラのプレビューになる。

材料の入手経路だけが収集ジョブと違う。計測 → フィット → 診断 → 保存は両ジョブで同じなので
:mod:`web.api.jobs.steps` の :func:`~web.api.jobs.steps.build_calibration` と :func:`~web.api.jobs.steps.report_calibration` を使う。
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from pcbasm.pasting.paste_volume.dataset.reader import (
    DatasetSession,
    completed_sessions,
)
from web.api.jobs.pasting import PasteStores
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult
from web.api.jobs.steps import (
    DETECTION_PARAM_NAMES,
    DETECTION_PARAMS,
    REQUIRE_BLANK_ZERO_PARAM,
    build_calibration,
    report_calibration,
    validate_detection_params,
)

# ハイパラ探索の途中経過を見るために「保存しない」を選べる
_SAVE_NAME_PARAM = ParamSpec(
    "save_name",
    "校正の保存名",
    "str",
    default="",
    optional=True,
    help=(
        "空なら保存せず診断だけを出します。"
        "指定するとその名前で校正ファイルを保存します。"
    ),
)


def register(catalog: JobCatalog, stores: PasteStores) -> None:
    catalog.register(
        JobDefinition(
            name="paste_volume_refit",
            label="塗布量校正の再フィット",
            run=partial(_run_paste_volume_refit, stores=stores),
            params=(
                *DETECTION_PARAMS,
                REQUIRE_BLANK_ZERO_PARAM,
                _SAVE_NAME_PARAM,
            ),
            requires_pcb=False,
            uses_machine=False,
            persisted_params=(*DETECTION_PARAM_NAMES, "require_blank_zero"),
        )
    )


def _run_paste_volume_refit(ctx: JobContext, *, stores: PasteStores) -> JobResult:
    """選んだ session を計測してフィットし、校正ファイルと診断図を出す."""
    error = validate_detection_params(ctx.params)
    if error is not None:
        raise ValueError(error)

    session = _choose_session(ctx, stores.paste_dataset_dir)
    ctx.progress("計測とフィット", None)

    fit, error = build_calibration(ctx, session)
    if fit is None:
        raise ValueError(error)
    summary, artifacts = report_calibration(
        ctx, session, fit, calibration_dir=stores.paste_volume_calibration_dir
    )
    return JobResult(
        summary=(
            f"校正生成完了: {fit.calibration.label} / "
            f"塗布 {fit.calibration.source.sample_count} 点 / {summary}"
        ),
        artifacts=artifacts,
    )


def _choose_session(ctx: JobContext, dataset_dir: Path) -> DatasetSession:
    """完成 session を運転者に選ばせて読み込む."""
    sessions = completed_sessions(dataset_dir)
    if not sessions:
        raise ValueError(f"校正に使える完成datasetがありません: {dataset_dir}")
    answer = ctx.choose(
        "校正の材料にするdatasetを選んでください。",
        tuple(session.name for session in sessions),
        default=sessions[-1].name,
    )
    session, error = DatasetSession.load(dataset_dir / answer)
    if session is None:
        raise ValueError(error)
    return session
