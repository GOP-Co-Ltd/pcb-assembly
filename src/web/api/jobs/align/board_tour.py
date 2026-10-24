"""ボード巡回ジョブ（四隅巡回 → 銅箔照合 → 補正適用済みの全 pad 巡回）."""

from __future__ import annotations

import time
from collections.abc import Sequence

import attrs

from pcbasm.align import (
    AlignEvent,
    BoardCorrection,
    CopperProjector,
    PadMeasured,
    PadRefinement,
    PadResultRenderer,
    RefinementPlanned,
    RegionAlignmentSession,
    RegionMeasured,
    align_regions,
    refine_pad,
    refinement_targets,
    render_label,
)
from pcbasm.board import Layer, Pad
from pcbasm.geometry import Point2d, Shift, Transform, sort_by_nearest
from pcbasm.hal import Camera, Speed, XYZStage
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult
from web.api.jobs.steps import align_events, board_corners, move_to, setup_board

# 巡回先 1 点あたりのフレーム配信時間 [sec]
RESULT_DISPLAY_SEC = 1.0


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="board_tour",
            label="ボード巡回",
            run=_run_board_tour,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
            ),
            requires_pcb=True,
            uses_machine=True,
        )
    )


def _pad_renderer(
    session: RegionAlignmentSession,
    projector: CopperProjector,
    pads: Sequence[Pad],
    position: Point2d,
) -> PadResultRenderer:
    """Pad 群の照合結果 overlay 合成器を構築する."""
    return PadResultRenderer(
        projector=projector,
        edge_detector=session.edge_detector,
        roi=session.region_roi,
        paste_polygons=[p.polygon for p in pads],
        position=position,
    )


def _stream_labeled_frames(
    ctx: JobContext,
    camera: Camera,
    label: str,
    duration: float = RESULT_DISPLAY_SEC,
) -> None:
    """ラベル付きフレームを duration 秒間プレビューへ配信する."""
    crop_size = ctx.machine.camera.crop.size
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        ctx.frame(render_label(camera.capture(), crop_size, label))
        ctx.checkpoint()


def _stream_pad_result(
    ctx: JobContext,
    camera: Camera,
    renderer: PadResultRenderer,
    lines: list[str],
    duration: float = RESULT_DISPLAY_SEC,
) -> None:
    """Pad 照合結果 overlay を duration 秒間プレビューへ配信する."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        ctx.frame(renderer.render(camera.capture(), lines))
        ctx.checkpoint()


def _run_board_tour(ctx: JobContext) -> JobResult:
    """四隅巡回 → 銅箔照合 → 補正適用済み全 pad 巡回を実行する."""
    klipper = ctx.klipper()
    stage = XYZStage(klipper)
    with ctx.open_camera() as camera:
        board_frame = setup_board(ctx, klipper, stage, camera)
        board_transform = board_frame.board_transform

        # 四隅巡回（左上に戻る 5 点）
        corners = [
            *board_corners(board_frame.board.outline),
            ("Top-Left", Point2d(0.0, 0.0)),
        ]
        for index, (name, board_pt) in enumerate(corners):
            ctx.progress("四隅巡回", 100.0 * index / len(corners))
            machine_pt = board_transform.apply(board_pt)
            ctx.log(
                f"{name}: Board({board_pt.x:.1f}, {board_pt.y:.1f}) -> "
                f"Machine({machine_pt.x:.3f}, {machine_pt.y:.3f})"
            )
            move_to(klipper, stage, machine_pt)
            _stream_labeled_frames(ctx, camera, f"Corner: {name}")

        top_pads = [pad for pad in board_frame.board.pads if pad.layer == Layer.TOP]
        session = RegionAlignmentSession(
            board_frame,
            ctx.machine,
            klipper=klipper,
            stage=stage,
            camera=camera,
            frame_sink=ctx.frame,
        )
        regions = session.plan_regions([pad.center for pad in top_pads])
        log_event = align_events(ctx)

        def on_event(event: AlignEvent) -> None:
            log_event(event)
            if isinstance(event, RegionMeasured) and event.fit is None:
                region = event.region
                pads = [pad for pad in top_pads if region.covers(pad.center)]
                renderer = _pad_renderer(
                    session,
                    session.projector,
                    pads,
                    stage.get_position().to2d(),
                )
                lines = [f"Region {region.index + 1}/{len(regions)}", "FAILED"]
                _stream_pad_result(ctx, camera, renderer, lines)

        correction = align_regions(session, regions, on_event=on_event)

        # 補正適用済みの全 pad 巡回
        entries = _corrected_entries(
            board_transform, stage, session, correction, top_pads
        )
        max_short_side = ctx.machine.alignment.refine_max_short_side
        targets = {
            pad.ref
            for pad in refinement_targets(top_pads, max_short_side_mm=max_short_side)
        }
        on_event(RefinementPlanned(len(targets), len(top_pads), max_short_side))
        refinements: list[PadRefinement] = []
        for index, (pad, renderer_projector, target) in enumerate(entries):
            ctx.progress("補正巡回", 100.0 * index / len(entries))
            ctx.checkpoint()
            if pad.ref in targets:
                refinement = refine_pad(session, correction, pad)
                on_event(PadMeasured(refinement))
                refinements.append(refinement)
                if refinement.result is not None:
                    shift = Shift.from_point(refinement.result.displacement)
                    renderer_projector = session.projector.with_correction(shift)
                    target = shift.apply(board_transform.apply(pad.center))
            move_to(klipper, stage, target, speed=Speed.rate(0.5))
            renderer = _pad_renderer(session, renderer_projector, [pad], target)
            lines = [f"{pad.designator}.{pad.pad_number} {index + 1}/{len(entries)}"]
            _stream_pad_result(ctx, camera, renderer, lines)
        correction = attrs.evolve(correction, refinements=tuple(refinements))

        # board 原点へ戻して終了
        move_to(klipper, stage, board_transform.apply(Point2d(0.0, 0.0)))

    return JobResult(summary=f"{correction.summary()} / 補正巡回 {len(entries)} pads")


def _corrected_entries(
    board_transform: Transform,
    stage: XYZStage,
    session: RegionAlignmentSession,
    board_correction: BoardCorrection,
    pads: Sequence[Pad],
) -> list[tuple[Pad, CopperProjector, Point2d]]:
    """補正適用済みの pad 巡回先を nearest neighbor 順で構築する."""
    entries: list[tuple[Pad, CopperProjector, Point2d]] = []
    for pad in pads:
        correction = board_correction.correction_for(pad.center)
        corrected_projector = session.projector.with_correction(correction)
        target = correction.apply(board_transform.apply(pad.center))
        entries.append((pad, corrected_projector, target))

    current = stage.get_position()
    return sort_by_nearest(
        entries, current.to2d().to3d(), key=lambda entry: entry[2].to3d()
    )
