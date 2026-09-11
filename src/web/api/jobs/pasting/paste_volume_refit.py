"""収集済み session から塗布量校正を作り直すジョブ（装置不要）.

校正そのものは収集ジョブ（`paste_volume_calibration`）が最後に作る。こちらは
**検出ハイパラを変えて作り直す**ための経路で、装置も 1 時間の収集もペーストも
要らずに数秒で終わる。実行そのものがハイパラのプレビューになる。

計測・フィット・診断・保存は :mod:`web.api.jobs.pasting.paste_volume_common` と
:mod:`pcbasm.pasting.paste_volume` にあり、ここは session の選択 UI だけを担う。
"""

from __future__ import annotations

from pcbasm.pasting.dataset.reader import DatasetSession, completed_sessions
from web.api.jobs.catalog import JobCatalog, JobDefinition
from web.api.jobs.context import JobContext, JobResult, PromptSpec
from web.api.jobs.pasting.paste_volume_common import (
    DETECTION_PARAM_NAMES,
    DETECTION_PARAMS,
    REQUIRE_BLANK_ZERO_PARAM,
    SAVE_NAME_PARAM,
    build_calibration,
    calibration_result,
    validate_detection_params,
)


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="paste_volume_refit",
            label="塗布量校正の再フィット",
            tab="pasting",
            run=_run_paste_volume_refit,
            params=(
                *DETECTION_PARAMS,
                REQUIRE_BLANK_ZERO_PARAM,
                SAVE_NAME_PARAM,
            ),
            requires_pcb=False,
            uses_machine=False,
            persisted_params=(*DETECTION_PARAM_NAMES, "require_blank_zero"),
        )
    )


def _run_paste_volume_refit(ctx: JobContext) -> JobResult:
    """選んだ session を計測してフィットし、校正ファイルと診断図を出す."""
    error = validate_detection_params(ctx.params)
    if error is not None:
        raise ValueError(error)

    session = _choose_session(ctx)
    ctx.progress("計測とフィット", None)

    fit, error = build_calibration(ctx, session)
    if fit is None:
        raise ValueError(error)
    return calibration_result(ctx, session, fit)


def _choose_session(ctx: JobContext) -> DatasetSession:
    """完成 session を運転者に選ばせて読み込む."""
    sessions = completed_sessions(ctx.paste_dataset_dir)
    if not sessions:
        raise ValueError(
            f"校正に使える完成datasetがありません: {ctx.paste_dataset_dir}"
        )
    answer = ctx.prompt(
        PromptSpec(
            kind="choice",
            message="校正の材料にするdatasetを選んでください。",
            default=sessions[-1].name,
            choices=tuple(session.name for session in sessions),
        )
    )
    session, error = DatasetSession.load(ctx.paste_dataset_dir / str(answer))
    if session is None:
        raise ValueError(error)
    return session
