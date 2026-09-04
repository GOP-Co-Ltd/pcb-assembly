"""キャリブレーション用の矩形 PCB 生成ジョブ（装置・カメラ不要）."""

from __future__ import annotations

from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import JobContext, JobResult


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="generate_rect_pcb",
            label="キャリブレーション矩形 PCB 生成",
            tab="pasting",
            run=_run_generate_rect_pcb,
            params=(
                ParamSpec("width", "基板幅", "float", 40.0, unit="mm"),
                ParamSpec("height", "基板高さ", "float", 40.0, unit="mm"),
            ),
            uses_machine=False,
        )
    )


def _run_generate_rect_pcb(ctx: JobContext) -> JobResult:
    """Flow calibration 用の外形だけ矩形 PCB を生成する（装置・カメラ不要）."""
    # pcbnew 依存はジョブ実行時のみ（KiCAD 未導入でも webui は起動可）
    from pcbasm.pcb.generate import generate_rect_pcb, save_board

    width = float(ctx.params["width"])
    height = float(ctx.params["height"])

    ctx.progress("生成")
    filename = f"flow_calibration_rect_{width:g}x{height:g}.kicad_pcb"
    save_board(generate_rect_pcb(width, height), ctx.artifacts_dir / filename)
    ctx.log(f"生成: {filename}（{width:g}x{height:g} mm）")
    ctx.progress("完了", 100.0)

    return JobResult(
        summary=f"{width:g}x{height:g} mm の矩形 PCB を生成しました",
        artifacts=(ctx.artifact("キャリブレーション矩形 PCB", filename, "file"),),
    )
