"""MJPEG 配信・オーバーレイ・参照カウント・ジョブ用オーバーライドスロット."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Generator
from typing import Literal

import cv2

from pcbasm.config import Machine
from pcbasm.hal import FrameHub
from pcbasm.vision import (
    CalibrationResult,
    CircleDetector,
    CopperEdgeDetector,
    DetectedCircle,
    Image,
    ImageArray,
    draw_overlay,
)
from webui.state import AppState

type OverlayKind = Literal["none", "crosshair", "circle", "copper"]
type _Renderer = Callable[[Image], Image]

MJPEG_BOUNDARY = "frame"
MJPEG_MEDIA_TYPE = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"

_GREEN = (0, 255, 0)


def _draw_status_text(img: ImageArray, text: str) -> None:
    """黒縁取り + 白文字のステータステキストを左上に描画する（in-place）."""
    cv2.putText(img, text, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3)
    cv2.putText(img, text, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)


def _draw_detected_circle(
    img: ImageArray, circle: DetectedCircle, crop_size: tuple[int, int]
) -> None:
    """検出円・その中心・カメラ中心と結ぶ線を描画する（in-place）.

    circle.center はクロップ座標系（原点はクロップ左上）なので、 フル画像座標へ変換する。
    """
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2
    half_w, half_h = crop_size[0] // 2, crop_size[1] // 2

    circle_x = int(cx - half_w + circle.center.x)
    circle_y = int(cy - half_h + circle.center.y)
    radius = int(circle.radius)

    cv2.circle(img, (circle_x, circle_y), radius, (0, 0, 255), 2)
    cv2.circle(img, (circle_x, circle_y), 3, (0, 0, 255), -1)
    cv2.line(img, (cx, cy), (circle_x, circle_y), (255, 0, 0), 2)


class _CircleRenderer:
    """円検出オーバーレイ。検出は detect_fps に間引き、直近結果を再利用する."""

    def __init__(
        self,
        detector: CircleDetector | None,
        crop_size: tuple[int, int],
        detect_interval: float,
    ) -> None:
        self._detector = detector
        self._crop_size = crop_size
        self._detect_interval = detect_interval
        self._next_detect = 0.0
        self._circle: DetectedCircle | None = None

    def __call__(self, image: Image) -> Image:
        if self._detector is None:
            img = draw_overlay(image, self._crop_size).numpy()
            _draw_status_text(img, "no calibration")
            return Image(img)

        now = time.monotonic()
        if now >= self._next_detect:
            self._circle = self._detector.detect_nearest_center(image)
            self._next_detect = now + self._detect_interval

        offset = self._circle.offset.mm if self._circle is not None else None
        img = draw_overlay(image, self._crop_size, offset).numpy()
        if self._circle is not None:
            _draw_detected_circle(img, self._circle, self._crop_size)
        return Image(img)


class _CopperRenderer:
    """銅箔エッジオーバーレイ。検出は detect_fps に間引き、直近結果を再利用する."""

    def __init__(
        self,
        detector: CopperEdgeDetector,
        canny_low: float,
        canny_high: float,
        detect_interval: float,
    ) -> None:
        self._detector = detector
        self._text = f"canny: {canny_low:g} / {canny_high:g}"
        self._detect_interval = detect_interval
        self._next_detect = 0.0
        self._edges: ImageArray | None = None

    def __call__(self, image: Image) -> Image:
        now = time.monotonic()
        if now >= self._next_detect:
            self._edges = self._detector.detect_edges(image)
            self._next_detect = now + self._detect_interval

        img = image.numpy().copy()
        if self._edges is not None:
            img[self._edges > 0] = _GREEN
        _draw_status_text(img, self._text)
        return Image(img)


class PreviewService:
    """MJPEG 配信・オーバーレイ・参照カウント・ジョブ用オーバーライドスロット."""

    def __init__(
        self,
        state: AppState,
        *,
        max_fps: float = 15.0,
        detect_fps: float = 5.0,
        override_ttl: float = 1.0,
        jpeg_quality: int = 80,
    ) -> None:
        """PreviewService を初期化する.

        Args:
            state: アプリケーション状態（FrameHub の供給元）
            max_fps: 配信フレームレート上限。実効は min(カメラ fps, max_fps)
            detect_fps: circle / copper 検出の実行上限
            override_ttl: ジョブ提供フレームの優先時間 [sec]
            jpeg_quality: JPEG エンコード品質
        """
        self._state = state
        self._max_fps = max_fps
        self._detect_fps = detect_fps
        self._override_ttl = override_ttl
        self._jpeg_quality = jpeg_quality
        self._count_lock = threading.Lock()
        self._client_count = 0
        self._override_lock = threading.Lock()
        self._override: tuple[Image, float] | None = None

    @property
    def client_count(self) -> int:
        """現在のストリーム接続数（/api/state 用）."""
        with self._count_lock:
            return self._client_count

    def submit_override(self, image: Image) -> None:
        """ジョブ用オーバーライドスロットへ書き込む（最新 1 枚のみ保持）.

        Phase 3 の JobContext.frame() がこれを呼ぶ。直近 override_ttl 以内の
        フレームは生フレームより優先して配信される。
        """
        with self._override_lock:
            self._override = (image, time.monotonic())

    def mjpeg_stream(
        self,
        overlay: OverlayKind,
        canny_low: float | None = None,
        canny_high: float | None = None,
    ) -> Generator[bytes]:
        """MJPEG の multipart パート列を生成する同期ジェネレータ.

        開始時に参照カウントを +1 して hub を起動し、終了（GeneratorExit /
        例外含む）で -1、0 になれば hub を停止する。配信は実効 fps に
        間引くが、間引き中も capture は回す（USB バッファ滞留対策）。

        Args:
            overlay: オーバーレイ種別
            canny_low: overlay=copper の Canny 下側閾値（None は machine.toml 値）
            canny_high: overlay=copper の Canny 上側閾値（None は machine.toml 値）
        """
        hub = self._acquire()
        try:
            source = hub.subscribe()
            renderer = self._build_renderer(overlay, canny_low, canny_high)
            interval = self._emit_interval(source.resolution.fps)
            next_emit = time.monotonic()
            while True:
                frame = source.capture()
                now = time.monotonic()
                if now < next_emit:
                    continue
                next_emit = now + interval
                image = self._current_override()
                if image is None:
                    image = renderer(frame)
                yield self._encode_part(image)
        finally:
            self._release(hub)

    def snapshot(
        self,
        overlay: OverlayKind,
        canny_low: float | None = None,
        canny_high: float | None = None,
    ) -> bytes:
        """1 フレーム取得してオーバーレイを描画し、JPEG bytes を返す.

        ストリーム未接続時は hub の start/stop が 1 回走る。
        """
        hub = self._acquire()
        try:
            renderer = self._build_renderer(overlay, canny_low, canny_high)
            frame = hub.subscribe().capture()
            return self._encode_jpeg(renderer(frame))
        finally:
            self._release(hub)

    def _acquire(self) -> FrameHub:
        """参照カウントを +1 し、現行 hub を起動して返す（start は冪等）."""
        hub = self._state.frame_hub()
        with self._count_lock:
            self._client_count += 1
            hub.start()
        return hub

    def _release(self, hub: FrameHub) -> None:
        """参照カウントを -1 し、0 になれば acquire 時の hub を停止する."""
        with self._count_lock:
            self._client_count -= 1
            if self._client_count == 0:
                hub.stop()

    def _current_override(self) -> Image | None:
        with self._override_lock:
            if self._override is None:
                return None
            image, timestamp = self._override
            if time.monotonic() - timestamp > self._override_ttl:
                return None
            return image

    def _emit_interval(self, camera_fps: float) -> float:
        fps = min(camera_fps, self._max_fps) if camera_fps > 0 else self._max_fps
        return 1.0 / fps

    def _build_renderer(
        self, overlay: OverlayKind, canny_low: float | None, canny_high: float | None
    ) -> _Renderer:
        """ストリーム開始時に machine 設定を 1 回読んでレンダラを構築する."""
        if overlay == "none":
            return lambda image: image

        machine = self._state.machine()
        crop_size = machine.camera.crop.size
        detect_interval = 1.0 / self._detect_fps

        match overlay:
            case "crosshair":
                return lambda image: draw_overlay(image, crop_size)
            case "circle":
                return _CircleRenderer(
                    self._build_circle_detector(machine), crop_size, detect_interval
                )
            case "copper":
                pad_align = machine.paste_dispenser.pad_align
                low = canny_low if canny_low is not None else pad_align.canny_low
                high = canny_high if canny_high is not None else pad_align.canny_high
                detector = CopperEdgeDetector(
                    canny_low=low,
                    canny_high=high,
                    blur_ksize=pad_align.blur_ksize,
                )
                return _CopperRenderer(detector, low, high, detect_interval)

    def _build_circle_detector(self, machine: Machine) -> CircleDetector | None:
        """円検出器を構築する（calibration が読めなければ None）."""
        try:
            calibration = CalibrationResult.load(machine.camera.calibration_file)
        except Exception:
            return None
        return CircleDetector(
            pixel_per_mm=calibration.pixel_per_mm,
            target_diameter_mm=machine.reference_point.target_diameter,
            crop_size=machine.camera.crop.size,
        )

    def _encode_jpeg(self, image: Image) -> bytes:
        ok, buffer = cv2.imencode(
            ".jpg", image.numpy(), [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality]
        )
        if not ok:
            raise RuntimeError("JPEG エンコードに失敗しました")
        return buffer.tobytes()

    def _encode_part(self, image: Image) -> bytes:
        """完全な multipart パート（boundary 行 + ヘッダ + JPEG + CRLF）を返す."""
        jpeg = self._encode_jpeg(image)
        header = (
            f"--{MJPEG_BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n\r\n"
        ).encode()
        return header + jpeg + b"\r\n"
