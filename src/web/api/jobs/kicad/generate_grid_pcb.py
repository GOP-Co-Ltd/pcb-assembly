"""グリッド PCB 生成ジョブ（直行性テスト用。装置・カメラ不要）."""

from __future__ import annotations

from pcbasm.kicad.build import generate_grid_pcb
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="generate_grid_pcb",
            label="グリッド PCB 生成",
            run=_run_generate_grid_pcb,
            params=(
                ParamSpec("size", "基板の一辺", "float", 40.0, unit="mm"),
                ParamSpec(
                    "divisions", "グリッド分割数", "int", 3, help="パッド数 = n^2"
                ),
                ParamSpec("pad_size", "パッドの一辺", "float", 0.5, unit="mm"),
            ),
            uses_machine=False,
        )
    )


def _run_generate_grid_pcb(ctx: JobContext) -> JobResult:
    """直行性テスト用グリッド PCB を生成する（装置・カメラ不要）."""
    size = float(ctx.params["size"])
    divisions = int(ctx.params["divisions"])
    pad_size = float(ctx.params["pad_size"])

    ctx.progress("生成")
    filename = f"grid_{divisions}x{divisions}.kicad_pcb"
    generate_grid_pcb(size, divisions, pad_size, ctx.artifacts_dir / filename)
    ctx.log(f"生成: {filename}（{size}x{size} mm, {divisions ** 2} パッド）")
    ctx.progress("完了", 100.0)

    return JobResult(
        summary=f"{size:g}x{size:g} mm / {divisions}x{divisions} = "
        f"{divisions ** 2} パッド",
        artifacts=(ctx.artifact("グリッド PCB", filename, "file"),),
    )
