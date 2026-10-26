"""塗布カバレッジ PCB 生成ジョブ（fill の要件を網羅するフィクスチャ PCB を作る）."""

from __future__ import annotations

from pcbasm.kicad.build import build_fill_coverage_board, save_board
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="make_fill_coverage_pcb",
            label="塗布カバレッジ PCB 生成",
            run=_run_make_fill_coverage_pcb,
            uses_machine=False,
        )
    )


def _run_make_fill_coverage_pcb(ctx: JobContext) -> JobResult:
    """Fill 要件網羅フィクスチャ PCB を生成する."""
    ctx.progress("生成")
    filename = "fill_coverage.kicad_pcb"
    save_board(build_fill_coverage_board(), ctx.artifacts_dir / filename)
    ctx.log(f"生成: {filename}")
    ctx.progress("完了", 100.0)

    return JobResult(
        summary="fill 要件網羅フィクスチャを生成しました",
        artifacts=(ctx.artifact("Fill Coverage PCB", filename, "file"),),
    )
