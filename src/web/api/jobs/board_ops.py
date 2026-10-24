"""ボード計測セットアップ・巡回プロンプト・銅箔照合ループの共有処理."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pcbasm.align import (
    AlignmentRegion,
    BoardCalibrationResult,
    RegionAlignment,
    RegionAlignmentSession,
    setup_board_calibration,
)
from pcbasm.board import Board
from pcbasm.hal import Camera
from web.api.jobs.context import JobContext, PromptSpec


def setup_board(
    ctx: JobContext,
    camera: Camera,
    *,
    tolerance: float | None = None,
    board: Board | None = None,
) -> BoardCalibrationResult:
    """Progress("セットアップ") → ボード計測セットアップの定型.

    Args:
        ctx: 実行中ジョブのコンテキスト
        camera: 撮像に使うカメラ
        tolerance: 位置合わせ許容誤差。省略時は ``ctx.params["tolerance"]``
        board: 計測対象の基板。省略時は選択 PCB（``ctx.board()``）
    """
    if tolerance is None:
        tolerance = float(ctx.params["tolerance"])
    if board is None:
        board = ctx.board()
    ctx.progress("セットアップ")
    return setup_board_calibration(
        ctx.devices,
        machine=ctx.machine,
        board=board,
        tolerance=tolerance,
        camera=camera,
        frame_sink=ctx.frame,
        journal=ctx.journal,
    )


def confirm_next_point(
    ctx: JobContext, label: str, *, while_waiting: Callable[[], None] | None = None
) -> bool:
    """巡回先での確認プロンプトを出し、次へ進むなら True・終了なら False を返す.

    Args:
        ctx: 実行中ジョブのコンテキスト
        label: プロンプトに表示する巡回先のラベル
        while_waiting: 応答待ちの間ポーリング間隔ごとに呼ばれるコールバック
            （``JobContext.prompt`` へそのまま渡す）

    Raises:
        JobAborted: プロンプト待機中に abort された場合
    """
    return bool(
        ctx.prompt(
            PromptSpec(
                kind="confirm",
                message=f"{label}: ベルトテンションを調整し、確認できたら次へ進みます",
                default=True,
                true_label="次へ",
                false_label="終了",
            ),
            while_waiting=while_waiting,
        )
    )


def align_regions(
    ctx: JobContext,
    session: RegionAlignmentSession,
    regions: Sequence[AlignmentRegion],
    *,
    on_failure: Callable[[AlignmentRegion, int], None] | None = None,
) -> list[RegionAlignment]:
    """計画済み領域を巡回し、収束した照合結果だけを返す."""
    aligned: list[RegionAlignment] = []
    for index, region in enumerate(regions):
        ctx.progress("銅箔照合", 100.0 * index / len(regions))
        ctx.checkpoint()
        alignment = session.align(region)
        if alignment is None:
            ctx.log(f"警告: 領域 {region.index} の照合に失敗")
            if on_failure is not None:
                on_failure(region, index)
            continue
        displacement = alignment.displacement
        ctx.log(
            f"領域 {region.index}: dx={displacement.x:+.4f} "
            f"dy={displacement.y:+.4f} mm, "
            f"rms_distance={alignment.match.rms_distance_px:.2f} px, "
            f"passes={alignment.passes}"
        )
        aligned.append(alignment)
    return aligned
