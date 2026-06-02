"""マシン初期化からBoard変換計測までの共通セットアップ."""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import attrs
import cv2

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.geometry import Move, Point2d, Transform
from pcbasm.hal import Camera, Klipper, XYZStage, create_camera
from pcbasm.pcb import PcbFile
from pcbasm.posctrl.board import BoardTransformMeasurer
from pcbasm.posctrl.offset import OffsetTransformMeasurer
from pcbasm.posctrl.position import XYPositionAdjustor
from pcbasm.vision import (
    CalibrationResult,
    CircleDetector,
    draw_overlay,
    safe_move_distance,
)

logger = logging.getLogger(__name__)


class OffsetObserver:
    """カメラ画像からオフセットを検出・表示するcallable."""

    def __init__(
        self,
        detector: CircleDetector,
        camera: Camera,
        crop_size: tuple[int, int],
        window_name: str,
        sample_count: int = 30,
    ) -> None:
        self._detector = detector
        self._camera = camera
        self._crop_size = crop_size
        self._window_name = window_name
        self._sample_count = sample_count

    def __call__(self) -> Point2d:
        result = self._detector.detect_with_statistics(
            self._camera.capture() for _ in range(self._sample_count)
        )
        if result is None:
            raise RuntimeError("検出に失敗しました")

        display = draw_overlay(self._camera.capture(), self._crop_size, result.mean_mm)
        cv2.imshow(self._window_name, display.numpy())
        cv2.waitKey(1)

        return result.mean_mm


@attrs.frozen
class BoardCalibrationResult:
    """ボードキャリブレーション結果."""

    machine: Machine
    klipper: Klipper
    stage: XYZStage
    camera: Camera
    calibration: CalibrationResult
    offset_transform: Transform
    board_transform: Transform
    pcb: PcbFile


def setup_board_calibration(
    machine: Machine,
    pcb_file_path: Path,
    tolerance: float = 0.1,
    window_name: str = "Calibration",
) -> BoardCalibrationResult:
    """マシン初期化からBoard変換計測までの共通セットアップを実行する."""
    # PCBファイル読み込み
    logger.info("=== PCBファイル読み込み ===")
    pcb = PcbFile(pcb_file_path)
    outline = pcb.outline
    logger.info("PCBファイル: %s", pcb_file_path)
    logger.info("Board幅: %.3f mm", outline.width)
    logger.info("Board高さ: %.3f mm", outline.height)

    # Klipper接続
    logger.info("=== Klipper接続 ===")
    klipper = Klipper(host=machine.klipper.host, port=machine.klipper.port)
    logger.info("接続先: %s:%s", machine.klipper.host, machine.klipper.port)
    stage = XYZStage(klipper.readonly)

    # カメラ初期化
    logger.info("=== カメラ初期化 ===")
    cam_config = machine.camera
    camera = create_camera(
        device_id=cam_config.device_id,
        width=cam_config.width,
        height=cam_config.height,
        fps=cam_config.fps,
        format=cam_config.format,
        backend=cam_config.backend,
    )
    logger.info("カメラ: %s", camera.info.name)
    logger.info("解像度: %sx%s", cam_config.width, cam_config.height)

    # キャリブレーション結果読み込み
    logger.info("=== キャリブレーション読み込み ===")
    calibration = CalibrationResult.load(cam_config.calibration_file)
    logger.info("pixel/mm: %.2f", calibration.pixel_per_mm)

    # 円検出器初期化
    ref_config = machine.reference_point
    detector = CircleDetector(
        pixel_per_mm=calibration.pixel_per_mm,
        target_diameter_mm=ref_config.target_diameter,
        crop_size=cam_config.crop.size,
        diameter_tolerance_mm=0.5,
    )

    # ホーミング
    logger.info("=== ホーミング (G28) ===")
    klipper.send_gcode(gcode.homing(x=True, y=True, z=True) + gcode.wait_for_done())
    logger.info("ホーミング完了")

    # Reference Pointへ移動
    logger.info("=== Reference Point (top left) へ移動 ===")
    logger.info("目標位置: (%s, %s)", ref_config.x, ref_config.y)
    if calibration.z_position is not None:
        logger.info("キャリブレーションZ位置: %.3f mm", calibration.z_position)
    klipper.send_gcode(
        stage.to_gcode(Move(x=ref_config.x, y=ref_config.y, z=calibration.z_position))
        + gcode.wait_for_done()
    )
    logger.info("移動完了")
    time.sleep(1.0)

    # オフセット検出関数を定義
    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)

    observer = OffsetObserver(
        detector=detector,
        camera=camera,
        crop_size=cam_config.crop.size,
        window_name=window_name,
    )

    # カメラ回転角の計測（2点法）
    logger.info("=== カメラ回転角の計測 ===")
    move_distance = (
        safe_move_distance(cam_config.crop.size, margin=0.3) / calibration.pixel_per_mm
    )
    offset_transform_measurer = OffsetTransformMeasurer(
        observe_offset=observer,
        klipper=klipper,
        stage=stage,
        move_distance=move_distance,
    )
    offset_transform = offset_transform_measurer.measure()

    # 補正済みオフセット関数を定義
    def corrected_offset() -> Point2d:
        return offset_transform.apply(observer())

    # 位置補正を行い最終座標を返す関数を定義
    position_adjustor = XYPositionAdjustor(
        observe_offset=corrected_offset,
        klipper=klipper,
        stage=stage,
        tolerance=tolerance,
    )

    # Board変換の計測
    logger.info("=== Board変換の計測 ===")
    board_transform_measurer = BoardTransformMeasurer(
        adjust_reference=position_adjustor.adjust,
        klipper=klipper,
        stage=stage,
        outline=outline,
        reference_point=ref_config,
    )
    board_transform = board_transform_measurer.measure()

    return BoardCalibrationResult(
        machine=machine,
        klipper=klipper,
        stage=stage,
        camera=camera,
        calibration=calibration,
        offset_transform=offset_transform,
        board_transform=board_transform,
        pcb=pcb,
    )


@contextmanager
def machine_session(klipper: Klipper) -> Generator[None]:
    """マシンセッションのクリーンアップを管理するコンテキストマネージャ."""
    try:
        yield
    finally:
        klipper.send_gcode("M84")
        cv2.destroyAllWindows()
