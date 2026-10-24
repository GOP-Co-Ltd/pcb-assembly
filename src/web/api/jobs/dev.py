"""開発用のジョブ定義（PCB 抽出 / 塗布カバレッジ PCB 生成）."""

from __future__ import annotations

from pcbasm.board.export import save_copper, save_outline, save_pads, save_pnp_csv
from pcbasm.board.plot import render_pcb
from pcbasm.kicad.build import build_fill_coverage_board, save_board
from web.api.jobs.catalog import JobCatalog, JobDefinition
from web.api.jobs.context import JobContext, JobResult


def register_dev_jobs(catalog: JobCatalog) -> None:
    """開発用の 2 ジョブを登録する."""
    catalog.register(
        JobDefinition(
            name="extract_pcb",
            label="PCB 情報抽出",
            run=_run_extract_pcb,
            requires_pcb=True,
            uses_machine=False,
        )
    )
    catalog.register(
        JobDefinition(
            name="make_fill_coverage_pcb",
            label="塗布カバレッジ PCB 生成",
            run=_run_make_fill_coverage_pcb,
            uses_machine=False,
        )
    )


def _run_extract_pcb(ctx: JobContext) -> JobResult:
    """PCB から outline / 部品 / パッド / 銅箔を抽出し可視化 PNG を描く."""
    assert ctx.pcb_path is not None  # requires_pcb=True
    stem = ctx.pcb_path.stem

    ctx.progress("読込")
    ctx.log(f"PCBファイルを読み込み中: {ctx.pcb_path}")
    pcb = ctx.board()

    ctx.progress("抽出", 30.0)
    outline = pcb.outline
    ctx.log(f"サイズ: {outline.width:.2f} x {outline.height:.2f} mm")
    save_outline(outline, ctx.artifacts_dir / "outline.json")

    components = pcb.components
    save_pnp_csv(components, ctx.artifacts_dir / "pnp.csv")
    ctx.log(f"{len(components)} 部品 -> pnp.csv")

    pads = pcb.pads
    save_pads(pads, ctx.artifacts_dir / "pads.json")
    ctx.log(f"{len(pads)} パッド -> pads.json")

    copper = pcb.copper
    save_copper(copper, ctx.artifacts_dir / "copper.json")
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
            ctx.artifact("PCB 可視化", png_name, "image"),
            ctx.artifact("アウトライン", "outline.json", "file"),
            ctx.artifact("部品 (PnP)", "pnp.csv", "file"),
            ctx.artifact("パッド", "pads.json", "file"),
            ctx.artifact("銅箔", "copper.json", "file"),
        ),
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
