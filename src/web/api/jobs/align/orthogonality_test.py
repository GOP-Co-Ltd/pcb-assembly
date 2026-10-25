"""直行性テストジョブ（調整前の直行性指標を測り、四隅とグリッド交点を対話的に巡回する）."""

from __future__ import annotations

from collections.abc import Callable

from pcbasm.align import BoardFrame, OrthogonalityMetrics, render_label
from pcbasm.board import Layer
from pcbasm.geometry import Point2d, sort_by_nearest
from pcbasm.hal import Camera, XYZStage
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult
from web.api.jobs.steps import board_corners, move_to, setup_board


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="orthogonality_test",
            label="直行性テスト",
            run=_run_orthogonality_test,
            requires_pcb=True,
            uses_machine=True,
        )
    )


def _labeled_frame_sink(
    ctx: JobContext, camera: Camera, label: str
) -> Callable[[], None]:
    """ラベル付きライブフレームを 1 枚プレビューへ出すコールバックを作る.

    カメラ取得に失敗しても巡回を中断しない。最初の 1 回だけログへ警告し、以降はログを出さずに更新を見送る。
    """
    crop_size = ctx.machine.camera.crop.size
    warned = False

    def submit() -> None:
        nonlocal warned
        try:
            ctx.frame(render_label(camera.capture(), crop_size, label))
        except Exception as exc:
            if not warned:
                warned = True
                ctx.log(f"プレビュー更新に失敗しました（巡回は継続します）: {exc}")

    return submit


def _orthogonality_points(
    board_frame: BoardFrame, stage: XYZStage
) -> list[tuple[str, Point2d]]:
    """直行性テストの巡回点列（四隅 + TOP 層 pad 中心）を構築する.

    pad 中心は現在位置からの nearest neighbor 順に並べる（machine 座標で比較）。
    """
    board_transform = board_frame.board_transform
    top_pads = [p for p in board_frame.board.pads if p.layer == Layer.TOP]
    centers = sort_by_nearest(
        [p.center for p in top_pads],
        stage.get_position().to2d().to3d(),
        key=lambda center: board_transform.apply(center).to3d(),
    )
    return [
        *board_corners(board_frame.board.outline),
        *(
            (f"Grid {index + 1}/{len(centers)}", center)
            for index, center in enumerate(centers)
        ),
    ]


def _run_orthogonality_test(ctx: JobContext) -> JobResult:
    """調整前の直行性指標を計測し、四隅・グリッド交点を対話的に巡回する.

    各巡回先では確認プロンプトの応答を待つ間、ラベル付き overlay（十字線・
    関心領域・ラベル）を載せたライブフレームを配信し続ける。

    「次へ」で次の点へ進み、一周したら四隅から再開する。「終了」で正常終了する。
    巡回点列は開始時に 1 回だけ構築し、`Grid k/n` と pad の対応を周回間で固定する。
    指標は開始時の 1 回計測なので、巡回中のテンション調整は反映されない。

    Raises:
        JobAborted: 巡回中またはプロンプト待機中に abort された場合
    """
    total_points = 0
    cycle = 1
    finished = False
    klipper = ctx.klipper()
    stage = XYZStage(klipper)
    with ctx.open_camera() as camera:
        board_frame = setup_board(ctx, klipper, stage, camera)
        board_transform = board_frame.board_transform

        metrics = OrthogonalityMetrics.from_transform(board_transform)
        ctx.log("調整前の直行性指標:")
        ctx.log(f"軸間角の 90° からのずれ: {metrics.axis_angle_error_deg:+.3f} deg")
        ctx.log(f"スケール X: {metrics.scale_x:.5f} / Y: {metrics.scale_y:.5f}")
        ctx.log(
            "各巡回先で確認プロンプトが出ます。"
            "ベルトテンションを調整して「次へ」、やめるときは「終了」を押してください"
        )

        points = _orthogonality_points(board_frame, stage)
        while not finished:
            for index, (label, board_pt) in enumerate(points):
                ctx.progress(f"巡回 {cycle} 周目", 100.0 * index / len(points))
                ctx.checkpoint()
                machine_pt = board_transform.apply(board_pt)
                ctx.log(
                    f"{label}: Board({board_pt.x:.1f}, {board_pt.y:.1f}) -> "
                    f"Machine({machine_pt.x:.3f}, {machine_pt.y:.3f})"
                )
                move_to(klipper, stage, machine_pt)
                total_points += 1
                if not ctx.confirm(
                    f"{label}: ベルトテンションを調整し、確認できたら次へ進みます",
                    true_label="次へ",
                    false_label="終了",
                    while_waiting=_labeled_frame_sink(ctx, camera, label),
                ):
                    finished = True
                    break
            else:
                cycle += 1

    return JobResult(
        summary=(
            f"調整前の軸間角ずれ {metrics.axis_angle_error_deg:+.3f} deg / "
            f"調整前 scale X {metrics.scale_x:.5f} Y {metrics.scale_y:.5f} / "
            f"巡回 {total_points} 点（{cycle} 周目で終了）"
        )
    )
