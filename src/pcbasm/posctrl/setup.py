"""マシン初期化からBoard変換計測までの共通セットアップ."""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import attrs

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.geometry import Shift, Transform
from pcbasm.hal import Camera, Klipper, XYZStage, create_camera
from pcbasm.pcb import PcbFile
from pcbasm.posctrl.board import BoardTransformMeasurer
from pcbasm.posctrl.offset import OffsetTransformMeasurer
from pcbasm.posctrl.position import XYPositionAdjustor
from pcbasm.vision import (
    CalibrationResult,
    CircleDetector,
    FrameSink,
    draw_overlay,
    safe_move_distance,
)

logger = logging.getLogger(__name__)


class OffsetObserver:
    """カメラ画像からオフセットを検出・表示するobserver.

    observe() -> Transform 契約（カメラmm空間、原点=画像中心、想定→観測）。
    """

    def __init__(
        self,
        detector: CircleDetector,
        camera: Camera,
        crop_size: tuple[int, int],
        *,
        frame_sink: FrameSink | None = None,
        sample_count: int = 30,
    ) -> None:
        """OffsetObserverを初期化する.

        Args:
            detector: 円検出器
            camera: カメラ
            crop_size: 関心領域サイズ (width, height)
            frame_sink: 検出成功時に注釈画像を送る sink。Noneの場合は送らない
            sample_count: 統計検出に使うフレーム数
        """
        self._detector = detector
        self._camera = camera
        self._crop_size = crop_size
        self._frame_sink = frame_sink
        self._sample_count = sample_count

    def observe(self) -> Transform:
        """円検出オフセットを想定→観測のTransformとして返す.

        Raises:
            RuntimeError: 検出に失敗した場合
        """
        result = self._detector.detect_with_statistics(
            self._camera.capture() for _ in range(self._sample_count)
        )
        if result is None:
            raise RuntimeError("検出に失敗しました")

        if self._frame_sink is not None:
            display = draw_overlay(
                self._camera.capture(), self._crop_size, result.mean_mm
            )
            self._frame_sink(display)

        return Shift(result.mean_mm.x, result.mean_mm.y)


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
    *,
    camera: Camera | None = None,
    frame_sink: FrameSink | None = None,
) -> BoardCalibrationResult:
    """マシン初期化からBoard変換計測までの共通セットアップを実行する.

    Args:
        machine: マシン設定
        pcb_file_path: KiCADファイルのパス
        tolerance: 位置合わせの許容誤差 (mm)
        camera: 使用するカメラ。Noneの場合はマシン設定から生成する
        frame_sink: 検出注釈画像を送る sink。Noneの場合は表示しない
    """
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
    if camera is None:
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
        stage.move(x=ref_config.x, y=ref_config.y, z=calibration.z_position)
        + gcode.wait_for_done()
    )
    logger.info("移動完了")
    time.sleep(1.0)

    # オフセット検出関数を定義
    observer = OffsetObserver(
        detector=detector,
        camera=camera,
        crop_size=cam_config.crop.size,
        frame_sink=frame_sink,
    )

    # カメラ回転角の計測（2点法）
    logger.info("=== カメラ回転角の計測 ===")
    move_distance = (
        safe_move_distance(cam_config.crop.size, margin=0.3) / calibration.pixel_per_mm
    )
    offset_transform_measurer = OffsetTransformMeasurer(
        observe=observer.observe,
        klipper=klipper,
        stage=stage,
        move_distance=move_distance,
    )
    offset_transform = offset_transform_measurer.measure()

    # 位置補正を行い最終座標を返す関数を定義
    position_adjustor = XYPositionAdjustor(
        observe=observer.observe,
        klipper=klipper,
        stage=stage,
        offset_transform=offset_transform,
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
    """マシンセッション終了時にPRESENT、無ければM84を送るコンテキストマネージャ."""
    try:
        yield
    finally:
        klipper.send_present_or_relax()
