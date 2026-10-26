"""キャリブレーション用の矩形 PCB 生成ジョブ（装置・カメラ不要）."""

from __future__ import annotations

from pcbasm.pasting.plate import write_rect_board
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="generate_rect_pcb",
            label="キャリブレーション矩形 PCB 生成",
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
    width = float(ctx.params["width"])
    height = float(ctx.params["height"])

    ctx.progress("生成")
    filename = f"flow_calibration_rect_{width:g}x{height:g}.kicad_pcb"
    write_rect_board(ctx.artifacts_dir / filename, width, height)
    ctx.log(f"生成: {filename}（{width:g}x{height:g} mm）")
    ctx.progress("完了", 100.0)

    return JobResult(
        summary=f"{width:g}x{height:g} mm の矩形 PCB を生成しました",
        artifacts=(ctx.artifact("キャリブレーション矩形 PCB", filename, "file"),),
    )
