"""カメラ画像から、検出した対象の画像中心からのずれを観測する."""

from __future__ import annotations

import logging
import math
import time

from pcbasm.geometry import Shift, Transform
from pcbasm.hal import Camera
from pcbasm.vision import CameraModel, CenterOffsetDetector, FrameSink, draw_overlay

logger = logging.getLogger(__name__)

# 検出バッチの再取得（1 観測あたり）。機体差ではなく検出実装の都合なので設定に出さない。
# 基準円（`CircleDetector`）と塗布痕（`PasteDotDetector`）で同じ方針を使う
DETECTION_MAX_ATTEMPTS = 3
DETECTION_RETRY_SEC = 0.5


class CircleDetectionError(RuntimeError):
    """規定回数の再取得でも円検出の品質条件を満たさなかったときの例外."""


class OffsetObserver:
    """カメラ画像からオフセットを検出・表示する observer.

    observe() -> Transform 契約（カメラ mm 空間、原点=画像中心、想定→観測）に従う。
    """

    def __init__(
        self,
        detector: CenterOffsetDetector,
        camera: Camera,
        camera_model: CameraModel,
        crop_size: tuple[int, int],
        *,
        frame_sink: FrameSink | None = None,
        sample_count: int,
        minimum_sample_count: int,
        max_attempts: int = 1,
        retry_sec: float = 0.0,
        max_standard_deviation_mm: float | None = None,
    ) -> None:
        """OffsetObserver を初期化する.

        Args:
            detector: 検出器（Hough の :class:`~pcbasm.vision.CircleDetector` や
                塗布痕用の :class:`~pcbasm.vision.PasteDotDetector`）
            camera: カメラ
            camera_model: 検出した pixel のずれをカメラ mm へ写すカメラの写像
            crop_size: 関心領域サイズ (width, height)
            frame_sink: 検出成功時に注釈画像を送る sink。None の場合は送らない
            sample_count: 統計検出に使うフレーム数（``machine.detection.sample_count``）
            minimum_sample_count: 1 回の観測に必要な有効検出数
                （``machine.detection.minimum_sample_count``）
            max_attempts: 検出バッチの最大試行回数
            retry_sec: 再試行前の待機時間 [sec]
            max_standard_deviation_mm: 各軸の標準偏差上限。None なら制限しない
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
        self._camera_model = camera_model
        self._crop_size = crop_size
        self._frame_sink = frame_sink
        self._sample_count = sample_count
        self._minimum_sample_count = minimum_sample_count
        self._max_attempts = max_attempts
        self._retry_sec = retry_sec
        self._max_standard_deviation_mm = max_standard_deviation_mm

    def observe(self) -> Transform:
        """円検出オフセットを想定→観測の Transform として返す.

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
                mean_mm = self._camera_model.pixel_offset_to_camera(result.mean)
                std_mm = self._camera_model.pixel_offset_to_camera(result.std)
                if (
                    self._max_standard_deviation_mm is None
                    or max(std_mm.x, std_mm.y) <= self._max_standard_deviation_mm
                ):
                    if self._frame_sink is not None:
                        display = draw_overlay(
                            self._camera.capture(), self._crop_size, mean_mm
                        )
                        self._frame_sink(display)

                    # フレームごとのばらつきをログに残し、sample_count を調整する判断材料にする
                    logger.info(
                        "円検出: %d/%d フレーム, 標準偏差 X=%.4f Y=%.4f mm",
                        result.sample_count,
                        self._sample_count,
                        std_mm.x,
                        std_mm.y,
                    )
                    return Shift(mean_mm.x, mean_mm.y)
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
