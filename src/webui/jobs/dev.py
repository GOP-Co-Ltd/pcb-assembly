"""Dev タブのジョブ定義（PCB 抽出 / 塗布カバレッジ PCB 生成 / 機構検証デモ）.

pcbnew に依存するジョブは関数内で遅延 import する（KiCAD 未導入環境でも webui 自体は起動できるようにするため）。
"""

from __future__ import annotations

import time
from typing import Literal

import cv2
import numpy as np

from pcbasm.pcb import PcbFile
from pcbasm.vision import Image
from pcbasm.visualization import render_pcb
from webui.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from webui.jobs.context import JobContext, PromptSpec
from webui.jobs.manager import ApplyPayload, Artifact, JobResult


def register_dev_jobs(catalog: JobCatalog) -> None:
    """Dev タブの 2 ジョブと hidden の job_demo を登録する."""
    catalog.register(
        JobDefinition(
            name="extract_pcb",
            label="Extract PCB",
            tab="dev",
            run=_run_extract_pcb,
            requires_pcb=True,
            uses_machine=False,
        )
    )
    catalog.register(
        JobDefinition(
            name="make_fill_coverage_pcb",
            label="Make Fill Coverage Pcb",
            tab="dev",
            run=_run_make_fill_coverage_pcb,
            uses_machine=False,
        )
    )
    catalog.register(
        JobDefinition(
            name="job_demo",
            label="Job Demo",
            tab="dev",
            run=_run_job_demo,
            params=(
                ParamSpec("steps", "ステップ数", "int", 5),
                ParamSpec("interval", "ステップ間隔", "float", 0.2, unit="s"),
                ParamSpec("fail", "途中で失敗させる", "bool", False),
                ParamSpec("command_phase", "コマンド待機フェーズ", "bool", False),
            ),
            uses_machine=False,
            accepts_commands=True,
            hidden=True,
        )
    )


def _artifact(
    ctx: JobContext, label: str, filename: str, kind: Literal["image", "file"]
) -> Artifact:
    """ctx.artifacts_dir 直下のファイルを指す Artifact を作る."""
    return Artifact(label=label, path=f"{ctx.artifacts_dir.name}/{filename}", kind=kind)


def _run_extract_pcb(ctx: JobContext) -> JobResult:
    """PCB から outline / 部品 / パッド / 銅箔を抽出し可視化 PNG を描く."""
    assert ctx.pcb_path is not None  # requires_pcb=True
    stem = ctx.pcb_path.stem

    ctx.progress("読込")
    ctx.log(f"PCBファイルを読み込み中: {ctx.pcb_path}")
    pcb = PcbFile(ctx.pcb_path)

    ctx.progress("抽出", 30.0)
    outline = pcb.outline
    ctx.log(f"サイズ: {outline.width:.2f} x {outline.height:.2f} mm")
    outline.save(ctx.artifacts_dir / "outline.json")

    components = pcb.components
    components.save(ctx.artifacts_dir / "pnp.csv")
    ctx.log(f"{len(components)} 部品 -> pnp.csv")

    pads = pcb.pads
    pads.save(ctx.artifacts_dir / "pads.json")
    ctx.log(f"{len(pads)} パッド -> pads.json")

    copper = pcb.copper
    copper.save(ctx.artifacts_dir / "copper.json")
    ctx.log(f"{len(copper)} 銅箔島 -> copper.json")

    ctx.checkpoint()
    ctx.progress("描画", 70.0)
    png_name = f"{stem}_pcb.png"
    render_pcb(outline, pads, copper, components, ctx.artifacts_dir / png_name)
    ctx.log(f"画像 -> {png_name}")
    ctx.progress("完了", 100.0)

    return JobResult(
        summary=(
            f"{outline.width:.1f}x{outline.height:.1f}mm, "
            f"部品 {len(components)} / パッド {len(pads)} / 銅箔 {len(copper)}"
        ),
        artifacts=(
            _artifact(ctx, "PCB 可視化", png_name, "image"),
            _artifact(ctx, "アウトライン", "outline.json", "file"),
            _artifact(ctx, "部品 (PnP)", "pnp.csv", "file"),
            _artifact(ctx, "パッド", "pads.json", "file"),
            _artifact(ctx, "銅箔", "copper.json", "file"),
        ),
    )


def _run_make_fill_coverage_pcb(ctx: JobContext) -> JobResult:
    """Fill 要件網羅フィクスチャ PCB を生成する."""
    # pcbnew 依存はジョブ実行時のみ（KiCAD 未導入でも webui は起動可）
    from pcbasm.pcb.generate import build_fill_coverage_board, save_board

    ctx.progress("生成")
    filename = "fill_coverage.kicad_pcb"
    save_board(build_fill_coverage_board(), ctx.artifacts_dir / filename)
    ctx.log(f"生成: {filename}")
    ctx.progress("完了", 100.0)

    return JobResult(
        summary="fill 要件網羅フィクスチャを生成しました",
        artifacts=(_artifact(ctx, "Fill Coverage PCB", filename, "file"),),
    )


def _demo_frame() -> Image:
    """ctx.frame() 経路の通電確認用フレームを作る."""
    img = np.full((240, 320, 3), 48, dtype=np.uint8)
    cv2.putText(
        img, "job_demo", (40, 130), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2
    )
    return Image(img)


def _run_job_demo(ctx: JobContext) -> JobResult:
    """ジョブ機構（log/progress/prompt/command/abort/apply）の検証用デモ.

    UI には出ない（hidden）が POST 可能。E2E とテストが使う。
    """
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

    if ctx.params["command_phase"]:
        ctx.log("コマンド待機中（quit で離脱）")
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
            values={"paste_dispenser.pad_align.canny_low": canny_low},
        ),
    )
