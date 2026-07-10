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


def pad_align_abort_message(
    failed_designators: Sequence[str], max_failures: int | None
) -> str | None:
    """照合失敗数が許容数を超えたときの中止メッセージを返す.

    Args:
        failed_designators: 照合に失敗した部品の designator 一覧
        max_failures: 許容する失敗部品数。None は無制限

    Returns:
        許容内（失敗数 <= 許容数）または無制限なら None。
        超過なら失敗数・許容数・全 designator を含むメッセージ文字列。
    """
    if max_failures is None or len(failed_designators) <= max_failures:
        return None
    return (
        f"銅箔照合の失敗部品数が許容数を超えました"
        f"（失敗 {len(failed_designators)} / 許容 {max_failures}）: "
        f"{', '.join(failed_designators)}。基板の向き・種類を確認してください"
    )


def align_component_groups(
    ctx: JobContext,
    session: PadAlignmentSession,
    groups: Sequence[ComponentPads],
    *,
    on_failure: Callable[[ComponentPads, int], None] | None = None,
    max_failures: int | None = None,
) -> list[tuple[ComponentPads, PadAlignmentResult]]:
    """部品単位の銅箔照合ループの共通骨格.

    部品ごとに progress("銅箔照合") → checkpoint → ``session.align`` →
    dx/dy/theta/mean_distance の log を行い、成功した (group, alignment) を
    集めて返す。失敗は警告 log の後 ``on_failure``（あれば）を呼んで続行する
    （board_tour が失敗 overlay の配信に使う）。

    Args:
        ctx: 実行中ジョブのコンテキスト
        session: 銅箔照合セッション
        groups: 照合対象の部品グループ
        on_failure: 照合失敗時に呼ぶコールバック（group, index）
        max_failures: 失敗部品数の許容数。超過した時点で ValueError を送出し
            即中止。None は無制限（board_tour が使用）

    Raises:
        ValueError: 失敗部品数が ``max_failures`` を超えた場合
    """
    aligned: list[tuple[ComponentPads, PadAlignmentResult]] = []
    failed: list[str] = []
    for index, group in enumerate(groups):
        ctx.progress("銅箔照合", 100.0 * index / len(groups))
        ctx.checkpoint()
        designator = group.component.designator
        alignment = session.align(group)
        if alignment is None:
            ctx.log(f"警告: {designator} の照合に失敗")
            if on_failure is not None:
                on_failure(group, index)
            failed.append(designator)
            message = pad_align_abort_message(failed, max_failures)
            if message is not None:
                raise ValueError(message)
            continue
        translation = alignment.translation
        ctx.log(
            f"{designator}: dx={translation.x:+.4f} dy={translation.y:+.4f} mm, "
            f"theta={alignment.rotation.degrees:+.3f} deg, "
            f"mean_distance={alignment.match.mean_distance_px:.2f} px"
        )
        aligned.append((group, alignment))
    return aligned
