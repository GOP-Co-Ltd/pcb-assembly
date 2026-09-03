"""ツールヘッドオフセット計測ジョブ."""

from __future__ import annotations

from datetime import datetime

from pcbasm import gcode
from pcbasm.pasting.toolhead_offset import (
    MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
    ProbedPoint,
    ToolheadOffsetDiagnostics,
    ToolheadOffsetFailure,
    ToolheadOffsetProcedure,
    ToolheadOffsetResult,
    ToolheadOffsetSample,
    plan_toolhead_offset_points,
    validate_paste_diameters,
)
from pcbasm.pcb import PcbFile
from web.api.jobs.board_ops import setup_board
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import (
    ApplyPayload,
    JobContext,
    JobResult,
)
from web.api.jobs.pasting.common import (
    LOADING_DEFAULT_AMOUNT,
    run_loading_loop,
)

_DIAGNOSTICS_FILENAME = "toolhead_offset_diagnostics.json"


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="toolhead_offset",
            label="ツールヘッドオフセット計測",
            tab="pasting",
            run=_run_toolhead_offset,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
                ParamSpec(
                    "dispense_amount",
                    "吐出量",
                    "float",
                    LOADING_DEFAULT_AMOUNT,
                    unit="uL",
                ),
                ParamSpec(
                    "loading_amount",
                    "ローディング既定量",
                    "float",
                    LOADING_DEFAULT_AMOUNT,
                    unit="uL",
                ),
                ParamSpec("lift_height", "吐出後の上昇高さ", "float", 5.0, unit="mm"),
                ParamSpec(
                    "paste_diameter_min", "検出円の最小直径", "float", 0.0, unit="mm"
                ),
                ParamSpec(
                    "paste_diameter_max", "検出円の最大直径", "float", 2.0, unit="mm"
                ),
                ParamSpec(
                    "point_count",
                    "計測点数",
                    "int",
                    10,
                    minimum=MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
                    help="基板の安全領域を左上から走査して自動配置します",
                ),
                ParamSpec(
                    "point_spacing",
                    "点間隔",
                    "float",
                    5.0,
                    unit="mm",
                    help="自動配置する計測点同士の最小距離です",
                ),
                ParamSpec(
                    "edge_margin",
                    "基板外周margin",
                    "float",
                    5.0,
                    unit="mm",
                    minimum=0,
                    help="ペースト外縁から基板外周・穴まで確保する距離です",
                ),
            ),
            requires_pcb=True,
            uses_machine=True,
            accepts_commands=True,
            persisted_params=(
                "tolerance",
                "dispense_amount",
                "loading_amount",
                "lift_height",
                "paste_diameter_min",
                "paste_diameter_max",
                "point_count",
                "point_spacing",
                "edge_margin",
            ),
            provides_preview=True,
            loading_param="loading_amount",
        )
    )


def _run_toolhead_offset(ctx: JobContext) -> JobResult:
    """複数点のペースト吐出と円検出からツールヘッドXYオフセットを計測する."""
    tolerance = float(ctx.params["tolerance"])
    lift_height = float(ctx.params["lift_height"])
    diameter_min = float(ctx.params["paste_diameter_min"])
    diameter_max = float(ctx.params["paste_diameter_max"])
    point_count = int(ctx.params["point_count"])
    point_spacing = float(ctx.params["point_spacing"])
    edge_margin = float(ctx.params["edge_margin"])
    diameter_error = validate_paste_diameters(diameter_min, diameter_max)
    if diameter_error is not None:
        raise ValueError(diameter_error)

    # 配置不能ならカメラやKlipperを開始する前に中止する。
    assert ctx.pcb_path is not None  # requires_pcb=True
    planned_points, plan_error = plan_toolhead_offset_points(
        PcbFile(ctx.pcb_path).outline.polygon,
        point_count=point_count,
        point_spacing=point_spacing,
        edge_margin=edge_margin,
        paste_diameter_max=diameter_max,
    )
    if planned_points is None:
        raise ValueError(plan_error)
    total_points = len(planned_points)
    ctx.log(
        f"計測点を {total_points} 点配置"
        f"（最小間隔 {point_spacing:g} mm / 外周margin {edge_margin:g} mm）"
    )

    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera, tolerance=tolerance)
        klipper = result.klipper
        stage = result.stage
        calibration = result.calibration
        procedure = ToolheadOffsetProcedure(
            result,
            tolerance=tolerance,
            lift_height=lift_height,
            diameter_min=diameter_min,
            diameter_max=diameter_max,
            point_spacing=point_spacing,
            frame_sink=ctx.frame,
        )

        # 高さ計測フェーズ: 全計測点を先にプローブし、後続フェーズで使う絶対Zを保存する。
        probed_points: list[ProbedPoint] = []
        for index, board_position in enumerate(planned_points, start=1):
            ctx.checkpoint()
            ctx.progress(
                f"高さ計測 {index}/{total_points}",
                100.0 * (index - 1) / (3 * total_points),
            )
            probed = procedure.probe(board_position)
            probed_points.append(probed)
            ctx.log(
                f"高さ {index}/{total_points}: "
                f"board=({board_position.x:.3f}, {board_position.y:.3f}) / "
                f"dispense=({probed.point.dispense.x:.3f}, "
                f"{probed.point.dispense.y:.3f}) / "
                f"surface Z={probed.surface_z:.4f}"
            )

        with procedure.applicator() as applicator:
            # ペーストフェーズの直前に一度だけロードする。
            klipper.send_gcode(stage.move(z=0.0) + gcode.wait_for_done())
            run_loading_loop(
                ctx, klipper, stage, applicator, focus_z=calibration.z_position
            )
            applicator.retract()

            dispense_amount = float(ctx.params["dispense_amount"])
            for index, probed in enumerate(probed_points, start=1):
                ctx.checkpoint()
                ctx.progress(
                    f"ペースト塗布 {index}/{total_points}",
                    100.0 * (total_points + index - 1) / (3 * total_points),
                )
                procedure.deposit(applicator, probed, amount_ul=dispense_amount)
                ctx.log(
                    f"塗布 {index}/{total_points}: "
                    f"dispense=({probed.point.dispense.x:.3f}, "
                    f"{probed.point.dispense.y:.3f}) / "
                    f"surface Z={probed.surface_z:.4f}"
                )

        # オフセット計測フェーズ: 全点の塗布完了後に画像で位置を計測する。
        roi_width, roi_height = procedure.roi_size
        ctx.log(
            f"円検出ROI: {point_spacing:g} x {point_spacing:g} mm"
            f"（{roi_width} x {roi_height} px）"
        )
        samples: list[ToolheadOffsetSample] = []
        failures: list[ToolheadOffsetFailure] = []
        diagnostics_path = ctx.artifacts_dir / _DIAGNOSTICS_FILENAME
        for index, probed in enumerate(probed_points, start=1):
            ctx.checkpoint()
            ctx.progress(
                f"オフセット計測 {index}/{total_points}",
                100.0 * (2 * total_points + index - 1) / (3 * total_points),
            )
            outcome = procedure.measure(index, probed)
            if isinstance(outcome, ToolheadOffsetFailure):
                failures.append(outcome)
                if outcome.image is not None:
                    outcome.image.save(ctx.artifacts_dir / outcome.image_filename)
                    ctx.frame(outcome.image, persist=True)
                _diagnostics(total_points, failures, samples).save(diagnostics_path)
                ctx.log(
                    f"オフセット計測 {index}/{total_points}: 円検出失敗のためスキップ"
                    f"（{outcome.reason}）"
                )
                ctx.log(
                    f"失敗画像: /artifacts/{ctx.artifacts_dir.name}/"
                    f"{outcome.image_filename}"
                )
                continue
            samples.append(outcome)
            board_position = probed.point.board
            ctx.log(
                f"オフセット {index}/{total_points}: "
                f"board=({board_position.x:.3f}, {board_position.y:.3f}) / "
                f"dispense=({outcome.dispense_position.x:.3f}, "
                f"{outcome.dispense_position.y:.3f}) / "
                f"camera=({outcome.camera_position.x:.3f}, "
                f"{outcome.camera_position.y:.3f}) / "
                f"offset=({outcome.offset.x:+.4f}, {outcome.offset.y:+.4f})"
            )

    if failures:
        _diagnostics(total_points, failures, samples).save(diagnostics_path)
        ctx.log(
            "円検出診断: "
            f"/artifacts/{ctx.artifacts_dir.name}/{diagnostics_path.name}"
        )

    # オフセット算出 & 保存
    offset_result = ToolheadOffsetResult.measure(
        samples,
        tolerance=tolerance,
        point_spacing=point_spacing,
        edge_margin=edge_margin,
        calibrated_at=datetime.now(),
    )
    if offset_result is None:
        raise RuntimeError(
            "ツールヘッドオフセットの有効な計測点が不足しています"
            f"（有効 {len(samples)} 点 / "
            f"最低 {MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT} 点）"
        )
    measured_offset = offset_result.offset
    standard_deviation = offset_result.standard_deviation
    offset_result.save(ctx.artifacts_dir / "toolhead_offset.json")
    if not offset_result.is_within_tolerance:
        ctx.log(
            "ばらつきの大きい計測結果: "
            f"/artifacts/{ctx.artifacts_dir.name}/toolhead_offset.json"
        )
        raise RuntimeError(
            "ツールヘッドオフセットの標準偏差が位置合わせ許容誤差を超えました"
            f"（X={standard_deviation.x:.4f}, Y={standard_deviation.y:.4f} mm / "
            f"上限={tolerance:.4f} mm）"
        )

    current_toolhead = result.machine.paste_dispenser.toolhead
    diff_x = measured_offset.x - current_toolhead.x
    diff_y = measured_offset.y - current_toolhead.y
    ctx.progress("完了", 100.0)
    artifacts = [
        ctx.artifact("計測結果 JSON", "toolhead_offset.json", "file"),
    ]
    if failures:
        artifacts.append(ctx.artifact("円検出診断 JSON", _DIAGNOSTICS_FILENAME, "file"))
        artifacts.extend(
            ctx.artifact(f"円検出失敗 {failure.index}", failure.image_filename, "image")
            for failure in failures
            if failure.image is not None
        )
    return JobResult(
        summary=(
            f"{len(samples)}/{total_points}点の平均オフセット "
            f"X={measured_offset.x:+.4f} Y={measured_offset.y:+.4f} mm"
            f"（標準偏差 X={standard_deviation.x:.4f} "
            f"Y={standard_deviation.y:.4f} mm / 現在設定との差 "
            f"dX={diff_x:+.4f} dY={diff_y:+.4f}）"
        ),
        artifacts=tuple(artifacts),
        apply=ApplyPayload(
            label=(
                f"[paste_dispenser.toolhead] x={measured_offset.x:.4f}, "
                f"y={measured_offset.y:.4f} を設定に反映"
            ),
            values={
                "paste_dispenser.toolhead.x": round(measured_offset.x, 4),
                "paste_dispenser.toolhead.y": round(measured_offset.y, 4),
            },
        ),
    )


def _diagnostics(
    total_points: int,
    failures: list[ToolheadOffsetFailure],
    samples: list[ToolheadOffsetSample],
) -> ToolheadOffsetDiagnostics:
    return ToolheadOffsetDiagnostics(
        requested_point_count=total_points,
        minimum_valid_point_count=MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
        failures=tuple(failures),
        successful_point_count=len(samples),
    )
