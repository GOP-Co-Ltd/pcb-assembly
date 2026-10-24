"""ボード計測セットアップ・巡回プロンプト・位置合わせの経過表示の共有処理."""

from __future__ import annotations

from collections.abc import Callable

from pcbasm.align import (
    AlignEvent,
    AlignStep,
    BoardFrame,
    measure_board_frame,
)
from pcbasm.board import Board
from pcbasm.hal import Camera, KlipperClient, XYZStage
from web.api.jobs.context import JobContext, PromptSpec


def setup_board(
    ctx: JobContext,
    klipper: KlipperClient,
    stage: XYZStage,
    camera: Camera,
    *,
    tolerance: float | None = None,
    board: Board | None = None,
) -> BoardFrame:
    """Progress("セットアップ") → ボード計測セットアップの定型.

    Args:
        ctx: 実行中ジョブのコンテキスト
        klipper: このジョブの Klipper への接続（``ctx.klipper()``）
        stage: klipper のステージ
        camera: 撮像に使うカメラ
        tolerance: 位置合わせ許容誤差。省略時は ``ctx.params["tolerance"]``
        board: 計測対象の基板。省略時は選択 PCB（``ctx.board()``）
    """
    if tolerance is None:
        tolerance = float(ctx.params["tolerance"])
    if board is None:
        board = ctx.board()
    ctx.progress("セットアップ")
    return measure_board_frame(
        board,
        ctx.machine,
        klipper=klipper,
        stage=stage,
        camera=camera,
        tolerance=tolerance,
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


def align_events(ctx: JobContext) -> Callable[[AlignEvent], None]:
    """位置合わせの経過を、進捗・中断の確認・照合ログへ流すコールバックを作る.

    コールバックは、照合の 1 件に取りかかる前に abort されていれば ``JobAborted`` を送出する。
    """

    def on_event(event: AlignEvent) -> None:
        match event:
            case AlignStep(stage=stage, index=index, total=total):
                ctx.progress(stage, 100.0 * index / total)
                ctx.checkpoint()
            case _:
                ctx.log(event.message)

    return on_event
