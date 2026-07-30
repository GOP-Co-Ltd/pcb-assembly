"""ボード計測セットアップ・巡回プロンプト・銅箔照合ループの共有処理."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from pcbasm.hal import Camera
from pcbasm.posctrl import (
    AlignmentRegion,
    BoardAlignment,
    BoardCalibrationResult,
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
    min_regions 未満なら中止し、足りていれば成功区数・変位の平均とばらつきを
    判定材料として log に出す。

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
        成功した領域計測から pad ごとの局所補正を引くルックアップ

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
            f"場合は min_sharpness を下げてください（region_size_px を広げると"
            f"拘束は増えますが局所変動を平均して鈍るので 200 px 程度までです）"
        )
    board = BoardAlignment(results=tuple(results))
    mean, spread = board.mean_displacement, board.displacement_spread
    ctx.log(
        f"局所補正: 成功 {len(results)}/{len(regions)} 区 / "
        f"平均変位 dx={mean.x:+.4f} dy={mean.y:+.4f} mm / "
        f"ばらつき sx={spread.x * 1000:.1f} sy={spread.y * 1000:.1f} um"
        f"（pad ごとに最近傍の区の補正を使います）"
    )
    unconverged = sum(1 for r in results if not r.converged)
    if unconverged:
        ctx.log(
            f"警告: {unconverged}/{len(results)} 領域が上限パス数でも"
            f"収束しませんでした（採用はしています）"
        )
    return board


def alignment_summary(alignment: BoardAlignment, planned_regions: int) -> str:
    """銅箔照合の結果を JobResult.summary の先頭に載せる 1 行にまとめる.

    Args:
        alignment: 照合結果から引く局所補正
        planned_regions: 計画した領域数（成功数の分母）

    Returns:
        成功区数と、変位の平均・ばらつき（ログ用の記述統計）の要約
    """
    mean, spread = alignment.mean_displacement, alignment.displacement_spread
    return (
        f"照合成功 {len(alignment.results)}/{planned_regions} 領域 / "
        f"局所補正 平均 dx={mean.x:+.4f} dy={mean.y:+.4f} mm"
        f"（ばらつき sx={spread.x * 1000:.1f} sy={spread.y * 1000:.1f} um）"
    )
