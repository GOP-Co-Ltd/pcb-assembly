"""ツールヘッドオフセット計測ジョブ."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from pcbasm import gcode
from pcbasm.geometry import (
    Identity,
    Point2d,
)
from pcbasm.pasting.applicator import (
    build_applicator,
)
from pcbasm.pasting.params import PasteParamsPatch
from pcbasm.pasting.probe import ProbeExecutor
from pcbasm.pasting.toolhead_offset import (
    MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
    ToolheadOffsetResult,
    ToolheadOffsetSample,
    plan_toolhead_offset_points,
)
from pcbasm.pcb import (
    PcbFile,
)
from pcbasm.posctrl import (
    CircleDetectionError,
    OffsetObserver,
    XYPositionAdjustor,
)
from pcbasm.vision import CircleDetector
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

_TOOLHEAD_OFFSET_MIN_FRAME_DETECTIONS = 5
_TOOLHEAD_OFFSET_DETECTION_MAX_ATTEMPTS = 3
_TOOLHEAD_OFFSET_DETECTION_RETRY_DELAY = 0.5


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
    if not 0 <= diameter_min < diameter_max:
        raise ValueError("検出円の直径は 0 <= 最小直径 < 最大直径 である必要があります")

    # 配置不能ならカメラやKlipperを開始する前に中止する。
    assert ctx.pcb_path is not None  # requires_pcb=True
    planned_points = plan_toolhead_offset_points(
        PcbFile(ctx.pcb_path).outline.polygon,
        point_count=point_count,
        point_spacing=point_spacing,
        edge_margin=edge_margin,
        paste_diameter_max=diameter_max,
    )
    total_points = len(planned_points)
    ctx.log(
        f"計測点を {total_points} 点配置"
        f"（最小間隔 {point_spacing:g} mm / 外周margin {edge_margin:g} mm）"
    )

    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera, tolerance=tolerance)
        machine = result.machine
        klipper = result.klipper
        stage = result.stage
        calibration = result.calibration
        probe_config = machine.probe
        probe_executor = ProbeExecutor(
            klipper=klipper,
            stage=stage,
            lift_height=probe_config.lift_height,
        )
        dispenser_config = machine.paste_dispenser
        toolhead_transform = dispenser_config.toolhead.to_transform()

        # 高さ計測フェーズ: 全計測点を先にプローブし、後続フェーズで使う絶対Zを保存する。
        measured_points: list[tuple[Point2d, Point2d, Point2d, float]] = []
        for index, board_position in enumerate(planned_points, start=1):
            ctx.checkpoint()
            camera_position = result.board_transform.apply(board_position)
            dispense_position = toolhead_transform.apply(camera_position)
            ctx.progress(
                f"高さ計測 {index}/{total_points}",
                100.0 * (index - 1) / (3 * total_points),
            )
            klipper.send_gcode(
                stage.move(x=dispense_position.x, y=dispense_position.y)
                + gcode.wait_for_done()
            )
            board_surface_z = probe_executor.probe()
            measured_points.append(
                (
                    board_position,
                    camera_position,
                    dispense_position,
                    board_surface_z,
                )
            )
            ctx.log(
                f"高さ {index}/{total_points}: "
                f"board=({board_position.x:.3f}, {board_position.y:.3f}) / "
                f"dispense=({dispense_position.x:.3f}, "
                f"{dispense_position.y:.3f}) / "
                f"surface Z={board_surface_z:.4f}"
            )

        with build_applicator(
            klipper, stage, dispenser_config, lift_height=lift_height
        ) as applicator:
            # ペーストフェーズの直前に一度だけロードする。
            klipper.send_gcode(stage.move(z=0.0) + gcode.wait_for_done())
            run_loading_loop(
                ctx, klipper, stage, applicator, focus_z=calibration.z_position
            )
            applicator.retract()

            dispense_amount = float(ctx.params["dispense_amount"])
            paste_height = applicator.default_params.paste_height_mm
            for index, (
                _board_position,
                _camera_position,
                dispense_position,
                board_surface_z,
            ) in enumerate(measured_points, start=1):
                ctx.checkpoint()
                ctx.progress(
                    f"ペースト塗布 {index}/{total_points}",
                    100.0 * (total_points + index - 1) / (3 * total_points),
                )
                # transform は Identity なので machine XY と絶対 Z（表面 + 塗布高さ）を渡す。
                applicator.deposit_at(
                    dispense_position,
                    amount_ul=dispense_amount,
                    transform=Identity(),
                    params=applicator.default_params.patched(
                        PasteParamsPatch(paste_height=board_surface_z + paste_height)
                    ),
                )
                ctx.log(
                    f"塗布 {index}/{total_points}: "
                    f"dispense=({dispense_position.x:.3f}, "
                    f"{dispense_position.y:.3f}) / "
                    f"surface Z={board_surface_z:.4f}"
                )

        # オフセット計測フェーズ: 全点の塗布完了後に画像で位置を計測する。
        paste_roi_side = max(1, round(point_spacing * calibration.pixel_per_mm))
        paste_roi_size = (paste_roi_side, paste_roi_side)
        ctx.log(
            f"円検出ROI: {point_spacing:g} x {point_spacing:g} mm"
            f"（{paste_roi_side} x {paste_roi_side} px）"
        )
        paste_detector = CircleDetector(
            pixel_per_mm=calibration.pixel_per_mm,
            target_diameter_mm=(diameter_min + diameter_max) / 2,
            crop_size=paste_roi_size,
            diameter_tolerance_mm=(diameter_max - diameter_min) / 2,
        )
        paste_observer = OffsetObserver(
            detector=paste_detector,
            camera=result.camera,
            crop_size=paste_roi_size,
            frame_sink=ctx.frame,
            minimum_sample_count=_TOOLHEAD_OFFSET_MIN_FRAME_DETECTIONS,
            max_attempts=_TOOLHEAD_OFFSET_DETECTION_MAX_ATTEMPTS,
            retry_delay=_TOOLHEAD_OFFSET_DETECTION_RETRY_DELAY,
            max_standard_deviation_mm=tolerance,
        )
        paste_adjustor = XYPositionAdjustor(
            observe=paste_observer.observe,
            klipper=klipper,
            stage=stage,
            offset_transform=result.offset_transform,
            tolerance=tolerance,
        )
        samples: list[ToolheadOffsetSample] = []
        diagnostic_path = ctx.artifacts_dir / "toolhead_offset_diagnostics.json"
        detection_failures: list[dict[str, Any]] = []
        diagnostics: dict[str, Any] = {
            "requested_point_count": total_points,
            "minimum_valid_point_count": MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
            "successful_point_count": 0,
            "failures": detection_failures,
        }
        failure_images: list[tuple[int, str]] = []
        for index, (
            board_position,
            camera_position,
            dispense_position,
            _board_surface_z,
        ) in enumerate(measured_points, start=1):
            ctx.checkpoint()
            ctx.progress(
                f"オフセット計測 {index}/{total_points}",
                100.0 * (2 * total_points + index - 1) / (3 * total_points),
            )
            klipper.send_gcode(
                stage.move(
                    x=camera_position.x,
                    y=camera_position.y,
                    z=calibration.z_position,
                )
                + gcode.wait_for_done()
            )
            time.sleep(1.0)
            try:
                camera_final_position = paste_adjustor.adjust()
            except CircleDetectionError as exc:
                failure_image = result.camera.capture().crop_center(paste_roi_size)
                filename = f"toolhead_offset_failure_{index:02d}.png"
                failure_image.save(ctx.artifacts_dir / filename)
                ctx.frame(failure_image, persist=True)
                failure_images.append((index, filename))
                detection_failures.append(
                    {
                        "index": index,
                        "board_position": {
                            "x": board_position.x,
                            "y": board_position.y,
                        },
                        "reason": str(exc),
                        "image": filename,
                    }
                )
                diagnostics["successful_point_count"] = len(samples)
                diagnostic_path.write_text(
                    json.dumps(diagnostics, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                ctx.log(
                    f"オフセット計測 {index}/{total_points}: 円検出失敗のためスキップ"
                    f"（{exc}）"
                )
                ctx.log(f"失敗画像: /artifacts/{ctx.artifacts_dir.name}/{filename}")
                continue
            sample = ToolheadOffsetSample.from_positions(
                board_position=board_position,
                dispense_position=dispense_position,
                camera_position=camera_final_position,
            )
            samples.append(sample)
            ctx.log(
                f"オフセット {index}/{total_points}: "
                f"board=({board_position.x:.3f}, {board_position.y:.3f}) / "
                f"dispense=({dispense_position.x:.3f}, "
                f"{dispense_position.y:.3f}) / "
                f"camera=({camera_final_position.x:.3f}, "
                f"{camera_final_position.y:.3f}) / "
                f"offset=({sample.offset.x:+.4f}, {sample.offset.y:+.4f})"
            )

    if failure_images:
        diagnostics["successful_point_count"] = len(samples)
        diagnostic_path.write_text(
            json.dumps(diagnostics, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        ctx.log(
            "円検出診断: " f"/artifacts/{ctx.artifacts_dir.name}/{diagnostic_path.name}"
        )
    if len(samples) < MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT:
        raise RuntimeError(
            "ツールヘッドオフセットの有効な計測点が不足しています"
            f"（有効 {len(samples)} 点 / "
            f"最低 {MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT} 点）"
        )

    # オフセット算出 & 保存
    offset_result = ToolheadOffsetResult.measure(
        samples,
        tolerance=tolerance,
        point_spacing=point_spacing,
        edge_margin=edge_margin,
        calibrated_at=datetime.now(),
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

    current_toolhead = dispenser_config.toolhead
    diff_x = measured_offset.x - current_toolhead.x
    diff_y = measured_offset.y - current_toolhead.y
    ctx.progress("完了", 100.0)
    artifacts = [
        ctx.artifact("計測結果 JSON", "toolhead_offset.json", "file"),
    ]
    if failure_images:
        artifacts.append(
            ctx.artifact("円検出診断 JSON", "toolhead_offset_diagnostics.json", "file")
        )
        artifacts.extend(
            ctx.artifact(f"円検出失敗 {index}", filename, "image")
            for index, filename in failure_images
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
