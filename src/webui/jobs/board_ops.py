"""ボード計測セットアップと銅箔照合ループの共有処理."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from pcbasm.hal import Camera
from pcbasm.posctrl import (
    BoardCalibrationResult,
    ComponentPads,
    PadAlignmentResult,
    PadAlignmentSession,
    setup_board_calibration,
)
from webui.jobs.context import JobContext


def setup_board(
    ctx: JobContext,
    camera: Camera,
    *,
    tolerance: float | None = None,
    pcb_path: Path | None = None,
) -> BoardCalibrationResult:
    """Progress("セットアップ") → ボード計測セットアップの定型.

    Args:
        ctx: 実行中ジョブのコンテキスト
        camera: 撮像に使うカメラ
        tolerance: 位置合わせ許容誤差。省略時は ``ctx.params["tolerance"]``
        pcb_path: 計測対象の PCB。省略時は選択 PCB（``ctx.pcb_path``）
    """
    if tolerance is None:
        tolerance = float(ctx.params["tolerance"])
    if pcb_path is None:
        assert ctx.pcb_path is not None  # requires_pcb=True
        pcb_path = ctx.pcb_path
    ctx.progress("セットアップ")
    return setup_board_calibration(
        machine=ctx.machine,
        pcb_file_path=pcb_path,
        tolerance=tolerance,
        camera=camera,
        frame_sink=ctx.frame,
    )


def align_component_groups(
    ctx: JobContext,
    session: PadAlignmentSession,
    groups: Sequence[ComponentPads],
    *,
    on_failure: Callable[[ComponentPads, int], None] | None = None,
) -> list[tuple[ComponentPads, PadAlignmentResult]]:
    """部品単位の銅箔照合ループの共通骨格.

    部品ごとに progress("銅箔照合") → checkpoint → ``session.align`` →
    dx/dy/theta/mean_distance の log を行い、成功した (group, alignment) を
    集めて返す。失敗は警告 log の後 ``on_failure``（あれば）を呼んで続行する
    （board_tour が失敗 overlay の配信に使う）。
    """
    aligned: list[tuple[ComponentPads, PadAlignmentResult]] = []
    for index, group in enumerate(groups):
        ctx.progress("銅箔照合", 100.0 * index / len(groups))
        ctx.checkpoint()
        designator = group.component.designator
        alignment = session.align(group)
        if alignment is None:
            ctx.log(f"警告: {designator} の照合に失敗")
            if on_failure is not None:
                on_failure(group, index)
            continue
        translation = alignment.translation
        ctx.log(
            f"{designator}: dx={translation.x:+.4f} dy={translation.y:+.4f} mm, "
            f"theta={alignment.rotation.degrees:+.3f} deg, "
            f"mean_distance={alignment.match.mean_distance_px:.2f} px"
        )
        aligned.append((group, alignment))
    return aligned
