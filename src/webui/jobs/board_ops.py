"""ボード計測セットアップと銅箔照合ループの共有処理."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from pcbasm.hal import Camera
from pcbasm.posctrl import (
    BoardCalibrationResult,
    PadAlignmentResult,
    PadAlignmentSession,
    PadRegion,
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
    failed_regions: Sequence[str], max_failures: int | None
) -> str | None:
    """照合失敗数が許容数を超えたときの中止メッセージを返す.

    Args:
        failed_regions: 照合に失敗した領域のラベル一覧
        max_failures: 許容する失敗領域数。None は無制限

    Returns:
        許容内（失敗数 <= 許容数）または無制限なら None。
        超過なら失敗数・許容数・全ラベルを含むメッセージ文字列。
    """
    if max_failures is None or len(failed_regions) <= max_failures:
        return None
    return (
        f"銅箔照合の失敗領域数が許容数を超えました"
        f"（失敗 {len(failed_regions)} / 許容 {max_failures}）: "
        f"{', '.join(failed_regions)}。基板の向き・種類を確認してください"
    )


def align_pad_regions(
    ctx: JobContext,
    session: PadAlignmentSession,
    regions: Sequence[PadRegion],
    *,
    on_failure: Callable[[PadRegion, int], None] | None = None,
    max_failures: int | None = None,
) -> list[tuple[PadRegion, PadAlignmentResult]]:
    """関心領域(ROI)単位の銅箔照合ループの共通骨格.

    領域ごとに progress("銅箔照合") → checkpoint → ``session.align`` →
    dx/dy/theta/mean_distance の log を行い、成功した (region, alignment) を
    集めて返す。失敗は警告 log の後 ``on_failure``（あれば）を呼んで続行する
    （board_tour が失敗 overlay の配信に使う）。

    Args:
        ctx: 実行中ジョブのコンテキスト
        session: 銅箔照合セッション
        regions: 照合対象の領域列
        on_failure: 照合失敗時に呼ぶコールバック（region, index）
        max_failures: 失敗領域数の許容数。超過した時点で ValueError を送出し
            即中止。None は無制限（board_tour が使用）

    Raises:
        ValueError: 失敗領域数が ``max_failures`` を超えた場合
    """
    aligned: list[tuple[PadRegion, PadAlignmentResult]] = []
    failed: list[str] = []
    for index, region in enumerate(regions):
        ctx.progress("銅箔照合", 100.0 * index / len(regions))
        ctx.checkpoint()
        designators = ", ".join(region.designators)
        alignment = session.align(region)
        if alignment is None:
            ctx.log(f"警告: {region.label} [{designators}] の照合に失敗")
            if on_failure is not None:
                on_failure(region, index)
            failed.append(region.label)
            message = pad_align_abort_message(failed, max_failures)
            if message is not None:
                raise ValueError(message)
            continue
        translation = alignment.translation
        ctx.log(
            f"{region.label} [{designators}]: "
            f"dx={translation.x:+.4f} dy={translation.y:+.4f} mm, "
            f"theta={alignment.rotation.degrees:+.3f} deg, "
            f"mean_distance={alignment.match.mean_distance_px:.2f} px"
        )
        aligned.append((region, alignment))
    return aligned
