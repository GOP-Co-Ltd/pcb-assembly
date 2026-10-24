"""ジョブ機構を通す検証用ジョブ ``job_demo``.

log / progress / prompt / command / abort / apply を 1 本のジョブで通す。

製品のカタログには無い。backend のテストと E2E が、起動した app のカタログへ登録して使う。
"""

import time

import cv2
import numpy as np

from pcbasm.vision import Image
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import ApplyPayload, JobContext, JobResult, PromptSpec


def register_job_demo(catalog: JobCatalog) -> None:
    """``job_demo`` を登録する（装置を使わないので実機も fake も要らない）."""
    catalog.register(
        JobDefinition(
            name="job_demo",
            label="ジョブデモ",
            run=_run_job_demo,
            params=(
                ParamSpec("steps", "ステップ数", "int", 5),
                ParamSpec("interval", "ステップ間隔", "float", 0.2, unit="s"),
                ParamSpec("fail", "途中で失敗させる", "bool", False),
                ParamSpec("command_phase", "コマンド待機フェーズ", "bool", False),
                ParamSpec(
                    "live_value", "実行中編集値", "float", 1.0, runtime_editable=True
                ),
            ),
            uses_machine=False,
            accepts_commands=True,
        )
    )


def _demo_frame() -> Image:
    """ctx.frame() 経路の通電確認用フレームを作る."""
    img = np.full((240, 320, 3), 48, dtype=np.uint8)
    cv2.putText(
        img, "job_demo", (40, 130), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2
    )
    return Image(img)


def _run_job_demo(ctx: JobContext) -> JobResult:
    """ログ・進捗・フレーム・prompt・command・apply を順に 1 回ずつ通す."""
    steps = int(ctx.params["steps"])
    interval = float(ctx.params["interval"])

    for index in range(steps):
        ctx.checkpoint()
        ctx.log(f"step {index + 1}/{steps}")
        ctx.progress("demo", 100.0 * (index + 1) / steps)
        time.sleep(interval)

    ctx.frame(_demo_frame())

    if ctx.params["fail"]:
        raise RuntimeError("デモ失敗（fail=True）")

    proceed = ctx.prompt(
        PromptSpec(
            kind="confirm",
            message="続行しますか?",
            default=True,
            true_label="続行",
            false_label="中止",
        )
    )
    ctx.log(f"confirm 応答: {proceed}")

    canny_low = ctx.prompt(
        PromptSpec(kind="number", message="canny_low を入力してください", default=60.0)
    )
    assert isinstance(canny_low, float)
    ctx.log(f"number 応答: {canny_low}")
    ctx.log(f"live_value = {ctx.params['live_value']}")

    if ctx.params["command_phase"]:
        ctx.log("コマンド待機中（quit で離脱）")
        ctx.notify_operator()
        while True:
            command = ctx.next_command(timeout=None)
            if command is None:
                continue
            if command["type"] == "quit":
                ctx.log("command: quit")
                break
            ctx.log(f"command: {command}")

    return JobResult(
        summary=f"demo 完了（canny_low = {canny_low:g}）",
        apply=ApplyPayload(
            label=f"canny_low = {canny_low:g} を設定に反映",
            values={"alignment.canny_low": canny_low},
        ),
    )
