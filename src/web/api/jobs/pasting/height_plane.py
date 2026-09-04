"""高さ平面計測ジョブ."""

from __future__ import annotations

import cv2

from pcbasm.geometry import sampling_diagnostics
from pcbasm.pasting.height import plan_probe_points
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import Layer, PcbFile
from pcbasm.vision import Image
from pcbasm.visualization import render_height_plane, render_planned_points
from web.api.jobs.board_ops import setup_board
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import JobAborted, JobContext, JobResult, PromptSpec


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="height_plane",
            label="高さ平面計測",
            tab="pasting",
            run=_run_height_plane,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
            ),
            requires_pcb=True,
            uses_machine=True,
            provides_preview=True,
        )
    )


def _run_height_plane(ctx: JobContext) -> JobResult:
    """計画点プレビュー → confirm → 高さ計測 → ヒートマップ生成を実行する."""
    assert ctx.pcb_path is not None  # requires_pcb=True
    pcb = PcbFile(ctx.pcb_path)
    top_coppers = [c for c in pcb.copper if c.layer == Layer.TOP]
    # 装置を触らずに計画点だけ出す（計測時と同じ規則）
    planned_points = plan_probe_points(
        top_coppers, pcb.outline.polygon, config=ctx.machine.probe
    )

    planned_path = ctx.artifacts_dir / "planned_points.png"
    render_planned_points(
        planned_points,
        pcb=pcb,
        title=(
            f"Planned probe points: {ctx.pcb_path.stem} ({len(planned_points)} points)"
        ),
        output_path=planned_path,
    )
    preview = cv2.imread(str(planned_path))
    if preview is not None:
        ctx.frame(Image(preview), persist=True)
    ctx.log(f"計測予定点: /artifacts/{ctx.artifacts_dir.name}/planned_points.png")
    diagnostics = sampling_diagnostics(
        planned_points, [c.polygon for c in top_coppers], pcb.outline.polygon
    )
    if diagnostics is not None:
        ctx.log(
            f"計画点 {diagnostics.point_count} 点 / "
            f"min_clearance {diagnostics.min_clearance:.3f} mm / "
            f"凸包/外形面積比 {diagnostics.hull_area_ratio:.3f}"
        )

    try:
        proceed = ctx.prompt(
            PromptSpec(
                kind="confirm",
                message=f"{len(planned_points)} 点を計測します。続行しますか?",
                default=True,
                true_label="続行",
                false_label="中止",
            )
        )
    finally:
        if preview is not None:
            ctx.clear_frame()
    if not proceed:
        raise JobAborted()

    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera)
        session = PasteSession.from_calibration(result)
        ctx.progress("高さ計測")
        height_plane = session.measure_height_plane(top_coppers)

    render_height_plane(
        height_plane,
        pcb=pcb,
        title=f"Height Plane: {ctx.pcb_path.stem}",
        output_path=ctx.artifacts_dir / "height_plane.png",
        pcb_to_plane=session.board_to_machine,
    )
    zs = [p.z for p in height_plane.points]
    return JobResult(
        summary=f"{len(zs)} 点計測 / Z {min(zs):.3f}〜{max(zs):.3f} mm",
        artifacts=(
            ctx.artifact("計測予定点", "planned_points.png", "image"),
            ctx.artifact("ヒートマップ", "height_plane.png", "image"),
        ),
    )
