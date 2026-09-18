"""マシン初期化からBoard変換計測までの共通セットアップ."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import attrs

from pcbasm.config import Machine
from pcbasm.gcode import GCode
from pcbasm.geometry import Shift, Transform
from pcbasm.hal import Camera, Klipper, XYZStage, create_camera
from pcbasm.parking import park_or_present
from pcbasm.pcb import PcbFile
from pcbasm.posctrl.board import BoardTransformMeasurer
from pcbasm.posctrl.offset import OffsetTransformMeasurer
from pcbasm.posctrl.position import XYPositionAdjustor
from pcbasm.vision import (
    CalibrationResult,
    CenterOffsetDetector,
    CircleDetector,
    FrameSink,
    draw_overlay,
    safe_move_distance,
)

logger = logging.getLogger(__name__)

# 検出バッチの再取得（1 観測あたり）。機体差ではなく検出実装の都合なので設定に出さない。
# 基準円（`CircleDetector`）と塗布痕（`PasteDotDetector`）で同じ方針を使う
DETECTION_MAX_ATTEMPTS = 3
DETECTION_RETRY_SEC = 0.5


class CircleDetectionError(RuntimeError):
    """規定回数の再取得でも円検出の品質条件を満たさなかった."""


class OffsetObserver:
    """カメラ画像からオフセットを検出・表示するobserver.

    observe() -> Transform 契約（カメラmm空間、原点=画像中心、想定→観測）。
    """

    def __init__(
        self,
        detector: CenterOffsetDetector,
        camera: Camera,
        crop_size: tuple[int, int],
        *,
        frame_sink: FrameSink | None = None,
        sample_count: int,
        minimum_sample_count: int,
        max_attempts: int = 1,
        retry_sec: float = 0.0,
        max_standard_deviation_mm: float | None = None,
    ) -> None:
        """OffsetObserverを初期化する.

        Args:
            detector: 検出器（Hough の :class:`~pcbasm.vision.CircleDetector` や
                塗布痕用の :class:`~pcbasm.vision.PasteDotDetector`）
            camera: カメラ
            crop_size: 関心領域サイズ (width, height)
            frame_sink: 検出成功時に注釈画像を送る sink。Noneの場合は送らない
            sample_count: 統計検出に使うフレーム数（``machine.detection.sample_count``）
            minimum_sample_count: 1回の観測に必要な有効検出数
                （``machine.detection.minimum_sample_count``）
            max_attempts: 検出バッチの最大試行回数
            retry_sec: 再試行前の待機時間 [sec]
            max_standard_deviation_mm: 各軸の標準偏差上限。Noneなら制限しない
        """
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count < 1
        ):
            raise ValueError("sample_countは1以上の整数である必要があります")
        if (
            isinstance(minimum_sample_count, bool)
            or not isinstance(minimum_sample_count, int)
            or minimum_sample_count < 1
            or minimum_sample_count > sample_count
        ):
            raise ValueError(
                "minimum_sample_countは1以上sample_count以下である必要があります"
            )
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attemptsは1以上の整数である必要があります")
        if not math.isfinite(retry_sec) or retry_sec < 0:
            raise ValueError("retry_secは0以上の有限値である必要があります")
        if max_standard_deviation_mm is not None and (
            not math.isfinite(max_standard_deviation_mm)
            or max_standard_deviation_mm <= 0
        ):
            raise ValueError(
                "max_standard_deviation_mmは正の有限値である必要があります"
            )

        self._detector = detector
        self._camera = camera
        self._crop_size = crop_size
        self._frame_sink = frame_sink
        self._sample_count = sample_count
        self._minimum_sample_count = minimum_sample_count
        self._max_attempts = max_attempts
        self._retry_sec = retry_sec
        self._max_standard_deviation_mm = max_standard_deviation_mm

    def observe(self) -> Transform:
        """円検出オフセットを想定→観測のTransformとして返す.

        Raises:
            CircleDetectionError: 規定回数の再取得でも検出品質を満たさない場合
        """
        failure_reason = "有効な円を検出できませんでした"
        for attempt in range(1, self._max_attempts + 1):
            result = self._detector.detect_with_statistics(
                (self._camera.capture() for _ in range(self._sample_count)),
                minimum_sample_count=self._minimum_sample_count,
            )
            if result is not None:
                std_mm = result.std_mm
                if (
                    self._max_standard_deviation_mm is None
                    or max(std_mm.x, std_mm.y) <= self._max_standard_deviation_mm
                ):
                    if self._frame_sink is not None:
                        display = draw_overlay(
                            self._camera.capture(), self._crop_size, result.mean_mm
                        )
                        self._frame_sink(display)

                    # フレーム毎のばらつきを残す。sample_count を詰める材料になる
                    logger.info(
                        "円検出: %d/%d フレーム, 標準偏差 X=%.4f Y=%.4f mm",
                        result.sample_count,
                        self._sample_count,
                        std_mm.x,
                        std_mm.y,
                    )
                    return Shift(result.mean_mm.x, result.mean_mm.y)
                failure_reason = (
                    "検出位置の標準偏差が上限を超えました"
                    f"（X={std_mm.x:.4f}, Y={std_mm.y:.4f} mm / "
                    f"上限={self._max_standard_deviation_mm:.4f} mm）"
                )
            else:
                failure_reason = (
                    f"{self._sample_count}フレーム中"
                    f"{self._minimum_sample_count}件以上の円を検出できませんでした"
                )

            logger.warning(
                "円検出の試行 %d/%d に失敗: %s",
                attempt,
                self._max_attempts,
                failure_reason,
            )
            if attempt < self._max_attempts and self._retry_sec > 0:
                time.sleep(self._retry_sec)

        raise CircleDetectionError(
            f"円検出に{self._max_attempts}回失敗しました: {failure_reason}"
        )


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
    klipper.send_gcode(GCode.homing(x=True, y=True, z=True) + GCode.wait_for_done())
    logger.info("ホーミング完了")

    # Reference Pointへ移動
    logger.info("=== Reference Point (top left) へ移動 ===")
    logger.info("目標位置: (%s, %s)", ref_config.x, ref_config.y)
    if calibration.z_position is not None:
        logger.info("キャリブレーションZ位置: %.3f mm", calibration.z_position)
    settle = machine.settle
    klipper.send_gcode(
        stage.move(x=ref_config.x, y=ref_config.y, z=calibration.z_position)
        + GCode.wait(settle.move_sec)
        + GCode.wait_for_done()
    )
    logger.info("移動完了")

    # オフセット検出関数を定義
    observer = OffsetObserver(
        detector=detector,
        camera=camera,
        crop_size=cam_config.crop.size,
        frame_sink=frame_sink,
        sample_count=machine.detection.sample_count,
        minimum_sample_count=machine.detection.minimum_sample_count,
        # 有効検出数の下限が効くようになったので、1 バッチ落ちただけで
        # ジョブの最初のステップが止まらないよう撮り直す（塗布痕側と同じ方針）
        max_attempts=DETECTION_MAX_ATTEMPTS,
        retry_sec=DETECTION_RETRY_SEC,
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
        settle_sec=settle.move_sec,
    )
    offset_transform = offset_transform_measurer.measure()

    # 位置補正を行い最終座標を返す関数を定義
    position_adjustor = XYPositionAdjustor(
        observe=observer.observe,
        klipper=klipper,
        stage=stage,
        offset_transform=offset_transform,
        tolerance=tolerance,
        settle_sec=settle.move_sec,
    )

    # Board変換の計測
    logger.info("=== Board変換の計測 ===")
    board_transform_measurer = BoardTransformMeasurer(
        adjust_reference=position_adjustor.adjust,
        klipper=klipper,
        stage=stage,
        outline=outline,
        reference_point=ref_config,
        settle_sec=settle.move_sec,
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
def machine_session(klipper: Klipper, machine: Machine) -> Generator[None]:
    """マシンセッション終了時にノズルキャップ駐機（フォールバックは PRESENT / M84）を行うコンテキストマネージャ."""
    try:
        yield
    finally:
        park_or_present(klipper, machine)
