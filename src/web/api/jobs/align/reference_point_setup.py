"""基準点設定ジョブ（基準点をジョグで合わせ、現在位置を設定へ保存する）."""

from __future__ import annotations

import time
from typing import Any

import cv2

from pcbasm.config import MachineConfig
from pcbasm.gcode import GCode
from pcbasm.geometry import Point3d
from pcbasm.hal import Camera, KlipperClient, XYZStage
from pcbasm.machine.commands import COMMAND_TIMEOUT
from pcbasm.vision import (
    CameraModel,
    CircleDetector,
    Image,
    draw_detected_circle,
    draw_overlay,
    reference_marker_detector,
)
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobAborted, JobContext
from web.api.jobs.runtime.record import JobResult
from web.api.jobs.steps import handle_machine_command

# 現在位置キャッシュ TTL [sec]
POSITION_CACHE_SEC = 0.5

_TEXT_COLOR = (0, 255, 255)  # 現在位置テキストの色 (BGR: 黄)


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="reference_point_setup",
            label="基準点設定",
            run=_run_reference_point_setup,
            uses_machine=True,
            accepts_commands=True,
        )
    )


class _CachedPosition:
    """stage.get_position() の短期キャッシュ（毎フレームの往復を回避）."""

    def __init__(self, stage: XYZStage, ttl: float = POSITION_CACHE_SEC) -> None:
        self._stage = stage
        self._ttl = ttl
        self._cached: Point3d | None = None
        self._expires = 0.0

    def get(self) -> Point3d:
        now = time.monotonic()
        if self._cached is None or now >= self._expires:
            self._cached = self._stage.get_position()
            self._expires = now + self._ttl
        return self._cached

    def invalidate(self) -> None:
        self._cached = None


def _run_reference_point_setup(ctx: JobContext) -> JobResult:
    """基準点 (top left) をジョグで合わせ、現在位置を設定へ保存する.

    マシン操作パネル（ジョブモード）の WS command でジョグし、record で現在位置を確定・即時反映、quit で中止する。
    """
    machine = ctx.machine
    klipper = ctx.klipper(COMMAND_TIMEOUT)
    stage = XYZStage(klipper)
    cam_config = machine.camera
    try:
        camera_model = CameraModel.load(cam_config.calibration_file)
    except Exception as exc:
        raise RuntimeError(f"カメラキャリブレーションが読み込めません: {exc}") from exc
    ref_config = machine.reference_point
    detector = reference_marker_detector(
        camera_model,
        cam_config.crop.size,
        target_diameter_mm=ref_config.target_diameter,
        diameter_tolerance_mm=ref_config.diameter_tolerance_mm,
    )

    ctx.progress("ホーミング")
    klipper.send_gcode(GCode.homing(x=True, y=True, z=True) + GCode.wait_for_done())
    ctx.log(f"カメラ Z 高さへ移動: {camera_model.focus_z:.3f} mm")
    klipper.send_gcode(stage.move(z=camera_model.focus_z) + GCode.wait_for_done())

    position = _CachedPosition(stage)
    with ctx.open_camera() as camera:
        ctx.progress("ジョグ待機")
        ctx.log("マシン操作パネルでジョグし、Record で現在位置を記録してください")
        ctx.notify_operator()
        while True:
            ctx.frame(
                _reference_point_frame(
                    camera, detector, camera_model, machine, position
                )
            )
            command = ctx.next_command(timeout=0)
            if command is not None and _dispatch_reference_command(
                ctx, klipper, stage, position, command
            ):
                break
            ctx.checkpoint()

    pos = stage.get_position()
    ctx.apply_machine_settings(
        {
            "reference_point.x": round(pos.x, 3),
            "reference_point.y": round(pos.y, 3),
        }
    )
    ctx.log(f"基準点を設定に反映しました: x={pos.x:.3f}, y={pos.y:.3f}")
    return JobResult(
        summary=f"基準点を設定に反映しました: x={pos.x:.3f}, y={pos.y:.3f} mm"
    )


def _reference_point_frame(
    camera: Camera,
    detector: CircleDetector,
    camera_model: CameraModel,
    machine: MachineConfig,
    position: _CachedPosition,
) -> Image:
    """円検出注釈 + 現在位置テキスト入りのプレビューフレームを合成する."""
    crop_size = machine.camera.crop.size
    image = camera.capture()
    result = detector.detect_nearest_center(image)
    offset = (
        camera_model.pixel_offset_to_camera(result.offset.px)
        if result is not None
        else None
    )
    preview = draw_overlay(image, crop_size, offset).numpy()
    if result is not None:
        draw_detected_circle(preview, result, crop_size)
    pos = position.get()
    cv2.putText(
        preview,
        f"Pos: X{pos.x:.3f}  Y{pos.y:.3f}",
        (10, 100),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        _TEXT_COLOR,
        2,
    )
    return Image(preview)


def _dispatch_reference_command(
    ctx: JobContext,
    klipper: KlipperClient,
    stage: XYZStage,
    position: _CachedPosition,
    command: dict[str, Any],
) -> bool:
    """WS command を 1 件処理する.

    Returns:
        record によりループを離脱する場合 True

    Raises:
        JobAborted: quit コマンドを受けた場合
    """
    match command:
        case {"type": "record"}:
            return True
        case {"type": "quit"}:
            ctx.log("中止しました。設定は変更していません")
            raise JobAborted()
    if handle_machine_command(ctx, klipper, stage, command):
        position.invalidate()
    else:
        ctx.log(f"未知のコマンドです: {command.get('type')!r}")
    return False
