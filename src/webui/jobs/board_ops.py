"""ボード計測セットアップ・巡回プロンプト・銅箔照合ループの共有処理."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from pcbasm.hal import Camera
from pcbasm.posctrl import (
    AlignmentRegion,
    BoardAlignment,
    BoardCalibrationResult,
    OrthogonalityMetrics,
    RegionAlignment,
    RegionAlignmentSession,
    setup_board_calibration,
)
from webui.jobs.context import JobContext, PromptSpec


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


def measure_regions(
    ctx: JobContext,
    session: RegionAlignmentSession,
    regions: Sequence[AlignmentRegion],
    *,
    min_regions: int,
    on_success: Callable[[RegionAlignment], None] | None = None,
    on_failure: Callable[[AlignmentRegion], None] | None = None,
) -> BoardAlignment:
    """領域単位の銅箔照合ループの共通骨格.

    領域ごとに progress("銅箔照合") → checkpoint → ``session.measure`` →
    dx/dy/rms/sharpness/passes の log。成功は ``on_success``（あれば）、失敗は
    警告 log の後 ``on_failure``（あれば）を呼んで続行する。最後に成功数が
    min_regions 未満なら中止し、足りていれば当てはめた補正モデル・スケール・
    スキュー・区ごとの残差を判定材料として log に出す。

    Args:
        ctx: 実行中ジョブのコンテキスト
        session: 銅箔照合セッション
        regions: 照合対象の領域（巡回順）
        min_regions: 成功が必要な最小領域数（1 以上）
        on_success: 照合成功時に呼ぶコールバック（board_tour が rms / sharpness の
            overlay に使う）。ステージは領域アンカーに留まっている
        on_failure: 照合失敗時に呼ぶコールバック（board_tour が FAILED
            overlay に使う）

    Returns:
        成功した領域計測から得た基板全体のアフィン補正

    Raises:
        ValueError: 計画領域数が min_regions 未満の場合（移動前に判定）、
            または成功領域数が min_regions 未満の場合
    """
    if len(regions) < min_regions:
        raise ValueError(
            f"照合領域を {len(regions)} 個しか計画できませんでした"
            f"（必要 {min_regions}）。領域は塗布対象 pad を含み、ROI 全体が"
            f"基板外形の内側（board_edge_margin）に収まるタイルだけを使います。"
            f"region_size_px を小さくするか board_edge_margin を下げ、"
            f"それでも足りなければ min_regions を下げてください"
        )
    results = []
    for index, region in enumerate(regions):
        ctx.progress("銅箔照合", 100.0 * index / len(regions))
        ctx.checkpoint()
        label = f"領域 {index + 1}/{len(regions)}"
        alignment = session.measure(region)
        if alignment is None:
            ctx.log(f"警告: {label} の照合に失敗")
            if on_failure is not None:
                on_failure(region)
            continue
        displacement, match = alignment.displacement, alignment.match
        ctx.log(
            f"{label}: "
            f"dx={displacement.x:+.4f} dy={displacement.y:+.4f} mm, "
            f"rms={match.rms_distance_px:.2f} px, "
            f"sharpness={match.sharpness:.3f}, "
            f"passes={alignment.passes} "
            f"(増分 {alignment.increment.norm * 1000:.1f} um)"
            + ("" if alignment.converged else " ※収束せず")
        )
        results.append(alignment)
        if on_success is not None:
            on_success(alignment)
    if len(results) < min_regions:
        raise ValueError(
            f"銅箔照合に成功した領域が不足しています"
            f"（成功 {len(results)} / 必要 {min_regions} / 計画 {len(regions)}）。"
            f"基板の向き・種類と照明、Canny 閾値（canny_low / canny_high）を"
            f"確認してください。ログの sharpness が min_sharpness を下回っている"
            f"場合は min_sharpness を下げるか region_size_px を大きくし、"
            f"それでも足りなければ min_regions を下げてください"
        )
    board = BoardAlignment(results=tuple(results))
    _log_alignment(ctx, board)
    return board


def _log_alignment(ctx: JobContext, board: BoardAlignment) -> None:
    """当てはめた補正の判定材料（モデル・スケール・スキュー・残差）を log に出す."""
    if board.model == "translation":
        remedy = (
            "区が 3 つ以上必要です"
            if len(board.results) < 3
            else "region_size_px を小さくして区の配置を広げてください"
        )
        ctx.log(
            f"警告: アンカーの広がりが不足（{len(board.results)} 区・最小主軸 "
            f"{board.fit.anchor_spread_mm:.2f} mm）のためアフィンを諦め"
            f"並進のみで補正します。{remedy}"
        )
    metrics = OrthogonalityMetrics.from_transform(board.machine_transform)
    translation = board.translation
    ctx.log(
        f"補正モデル: {board.model} / "
        f"並進 dx={translation.x:+.4f} dy={translation.y:+.4f} mm / "
        f"スケール x={(metrics.scale_x - 1) * 1e6:+.0f} "
        f"y={(metrics.scale_y - 1) * 1e6:+.0f} ppm / "
        f"スキュー {metrics.axis_angle_error_deg:+.4f} deg"
    )
    ctx.log(
        f"残差 RMS={board.residual_rms * 1000:.1f} um "
        f"最大={board.residual_max * 1000:.1f} um"
        f"（照合ノイズは区あたり 5um 級。数倍を超える場合は"
        f"非線形なひずみが残っている）"
    )
    for index, residual in enumerate(board.residuals):
        ctx.log(
            f"  領域 {index + 1}: "
            f"rx={residual.x * 1000:+.1f} ry={residual.y * 1000:+.1f} um"
        )
    unconverged = sum(1 for r in board.results if not r.converged)
    if unconverged:
        ctx.log(
            f"警告: {unconverged}/{len(board.results)} 領域が上限パス数でも"
            f"収束しませんでした（採用はしています）"
        )
