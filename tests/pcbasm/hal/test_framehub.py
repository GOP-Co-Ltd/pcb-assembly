"""`pcbasm.hal.framehub` の仕様テスト.

計画書 `memory/agents/implementation-planner/webui-phase2.md`
「src/pcbasm/hal/framehub.py」節 + spec §3 が契約:

- FrameHub はカメラ専有スレッドで capture し、最新フレームを複数消費者へ共有する
- `latest()` は初回到着まで待ち、停止後も最後のフレームがあれば待たずに返す
- `subscribe()` の FrameSource は Camera ABC を実装し「呼ぶたびに新しいフレーム」を保証
- キャプチャ例外は保持され、以降の `latest()` / `capture()` で繰り返し再送出。
  `start()` し直すとクリアされる
- start / stop は冪等。stop は camera を閉じない。シーケンスは stop/start で
  リセットされない（停止前からの FrameSource が再開後も使える）
- 新フレームが来ない状態（未 start・停止済み）では RuntimeError、timeout 超過は
  TimeoutError
"""

import threading
import time
from collections.abc import Callable
from typing import override

import numpy as np
import pytest

from pcbasm.hal import Camera, CameraInfo, FrameHub, Resolution, create_camera
from pcbasm.vision import CircleDetector, Image
from tests.helpers import (
    TESTING_DATA_DIR,
    FakeCamera,
    mark_hardware,
    skip_if_no_csi_camera,
)

FAKE_CAMERA_IMAGE = TESTING_DATA_DIR / "webui" / "fake_camera.png"


class CaptureFailure(Exception):
    """テスト用のキャプチャ例外（RuntimeError 系の契約例外と区別するため独立型）."""


def _solid_image(value: int) -> Image:
    return Image(np.full((8, 8, 3), value % 256, dtype=np.uint8))


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """条件成立までポーリングする（キャプチャスレッド終了などの観測用）."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("条件が時間内に成立しなかった")


class _StaticInfoCamera(Camera):
    """Resolution / info が固定のテスト用カメラ基底."""

    @property
    @override
    def resolution(self) -> Resolution:
        return Resolution(width=8, height=8, fps=30.0)

    @property
    @override
    def info(self) -> CameraInfo:
        return CameraInfo(name=type(self).__name__, formats={"BGR": [self.resolution]})


class GatedCamera(_StaticInfoCamera):
    """テスト側がゲートを開けるまで capture() がブロックするカメラ.

    フレーム供給タイミングを決定的に制御する。キャプチャスレッドを ブロックしたまま hub.stop() すると join
    が完了しないため、テスト終了時は 必ず release_all() してから hub.stop() を呼ぶこと。
    """

    def __init__(self) -> None:
        self._gate = threading.Semaphore(0)
        self._free_run = threading.Event()
        self._count = 0
        self._frame = _solid_image(0)

    def allow_frames(self, count: int = 1) -> None:
        """Capture() を count 回分だけ通過させる."""
        for _ in range(count):
            self._gate.release()

    def release_all(self) -> None:
        """以後の capture() をブロックさせない（テスト後始末用）."""
        self._free_run.set()
        self._gate.release()

    @override
    def capture(self) -> Image:
        if not self._free_run.is_set():
            if not self._gate.acquire(timeout=10.0):
                raise CaptureFailure("テストがゲートを開けないまま放置した")
            self._count += 1
            self._frame = _solid_image(self._count)
        return self._frame


class FailingCamera(_StaticInfoCamera):
    """Capture() が常に CaptureFailure を投げるカメラ."""

    @override
    def capture(self) -> Image:
        raise CaptureFailure("capture failed for test")


class FlakyCamera(_StaticInfoCamera):
    """初回の capture() だけ失敗し、以降は正常にフレームを返すカメラ."""

    def __init__(self) -> None:
        self._failed = False

    @override
    def capture(self) -> Image:
        if not self._failed:
            self._failed = True
            raise CaptureFailure("first capture failed")
        return _solid_image(1)


class TestFrameHubLifecycle:
    """Start / stop / latest のライフサイクル."""

    def test_start_makes_latest_return_frame_and_running_true(self):
        hub = FrameHub(FakeCamera([_solid_image(1)]))

        hub.start()
        try:
            assert hub.running
            assert isinstance(hub.latest(timeout=5.0), Image)
        finally:
            hub.stop()

    def test_latest_after_stop_returns_last_frame(self):
        hub = FrameHub(FakeCamera([_solid_image(1)]))
        hub.start()
        hub.latest(timeout=5.0)

        hub.stop()

        assert not hub.running
        assert isinstance(hub.latest(timeout=0.5), Image)

    def test_start_is_idempotent(self):
        hub = FrameHub(FakeCamera([_solid_image(1)]))

        hub.start()
        hub.start()
        try:
            assert hub.running
            assert isinstance(hub.latest(timeout=5.0), Image)
        finally:
            hub.stop()

    def test_stop_is_idempotent(self):
        hub = FrameHub(FakeCamera([_solid_image(1)]))
        hub.start()
        hub.latest(timeout=5.0)

        hub.stop()
        hub.stop()

        assert not hub.running

    def test_restart_after_stop_resumes_existing_subscriber(self):
        # シーケンスは stop/start でリセットされず、停止前からの FrameSource が
        # 再開後もそのまま capture を続行できる
        hub = FrameHub(FakeCamera([_solid_image(1)]))
        hub.start()
        source = hub.subscribe(timeout=5.0)
        source.capture()
        hub.stop()

        hub.start()
        try:
            assert isinstance(source.capture(), Image)
        finally:
            hub.stop()

    def test_stop_does_not_close_camera(self):
        camera = FakeCamera([_solid_image(1)])
        hub = FrameHub(camera)
        hub.start()
        hub.latest(timeout=5.0)

        hub.stop()

        assert isinstance(camera.capture(), Image)

    @mark_hardware
    @skip_if_no_csi_camera
    def test_real_csi_camera_smoke(self):
        camera = create_camera(
            device_id=0, width=1280, height=720, fps=30.0, backend="csi"
        )
        hub = FrameHub(camera)

        hub.start()
        try:
            assert hub.latest(timeout=10.0).size == (1280, 720)
            source_a = hub.subscribe(timeout=10.0)
            source_b = hub.subscribe(timeout=10.0)
            assert source_a.capture().size == (1280, 720)
            assert source_b.capture().size == (1280, 720)
        finally:
            hub.stop()

        assert not hub.running


class TestFrameSource:
    """Subscribe() が返す FrameSource（Camera ABC 実装）の契約."""

    def test_two_subscribers_receive_the_same_frame(self):
        camera = GatedCamera()
        hub = FrameHub(camera)
        hub.start()
        try:
            source_a = hub.subscribe(timeout=5.0)
            source_b = hub.subscribe(timeout=5.0)

            camera.allow_frames(1)
            frame_a = source_a.capture()
            frame_b = source_b.capture()

            # 同一シーケンスのフレームを共有する（奪い合いがない）
            assert frame_a is frame_b
        finally:
            camera.release_all()
            hub.stop()

    def test_capture_waits_for_newer_frame_and_never_repeats(self):
        camera = GatedCamera()
        hub = FrameHub(camera)
        hub.start()
        try:
            source = hub.subscribe(timeout=0.2)
            camera.allow_frames(1)
            first = source.capture()

            # 新フレームが供給されるまで capture は返らない（timeout 超過）
            with pytest.raises(TimeoutError):
                source.capture()

            camera.allow_frames(1)
            second = source.capture()

            # 同じフレームを 2 度返さない
            assert second is not first
        finally:
            camera.release_all()
            hub.stop()

    def test_resolution_and_info_delegate_to_camera(self):
        camera = FakeCamera([_solid_image(3)], fps=12.5)
        hub = FrameHub(camera)

        source = hub.subscribe()

        assert source.resolution == camera.resolution
        assert source.info == camera.info

    def test_frame_source_feeds_circle_detector_statistics(self):
        # Camera ABC 注入の契約: detect_with_statistics の 30 フレーム消費が
        # FrameSource で無改造に成立する
        camera = FakeCamera([Image.load(FAKE_CAMERA_IMAGE)])
        hub = FrameHub(camera)
        hub.start()
        try:
            source = hub.subscribe(timeout=5.0)
            detector = CircleDetector(
                pixel_per_mm=40.0, target_diameter_mm=3.0, crop_size=(600, 600)
            )
            stats = detector.detect_with_statistics(source.capture() for _ in range(30))
        finally:
            hub.stop()

        assert stats is not None
        assert stats.sample_count == 30


class TestErrors:
    """異常系: 未 start・停止済み・timeout・キャプチャ例外の伝播."""

    def test_latest_before_start_raises_runtime_error(self):
        hub = FrameHub(FakeCamera([_solid_image(1)]))

        with pytest.raises(RuntimeError):
            hub.latest(timeout=0.5)

    def test_capture_before_start_raises_runtime_error(self):
        hub = FrameHub(FakeCamera([_solid_image(1)]))
        source = hub.subscribe(timeout=0.5)

        with pytest.raises(RuntimeError):
            source.capture()

    def test_capture_after_stop_raises_runtime_error_once_drained(self):
        hub = FrameHub(FakeCamera([_solid_image(1)]))
        hub.start()
        source = hub.subscribe(timeout=0.5)
        source.capture()
        hub.stop()

        # 停止前に発行済みの残フレームは返り得るが、停止後シーケンスは進まないため
        # 有限回で必ず RuntimeError に到達する
        with pytest.raises(RuntimeError):
            for _ in range(100):
                source.capture()

    def test_latest_raises_timeout_error_when_no_frame_arrives(self):
        camera = GatedCamera()
        hub = FrameHub(camera)
        hub.start()
        try:
            with pytest.raises(TimeoutError):
                hub.latest(timeout=0.1)
        finally:
            camera.release_all()
            hub.stop()

    def test_capture_error_is_reraised_repeatedly(self):
        hub = FrameHub(FailingCamera())
        hub.start()
        source = hub.subscribe(timeout=2.0)

        with pytest.raises(CaptureFailure) as exc:
            hub.latest(timeout=2.0)
        assert "capture failed" in str(exc.value)

        # 繰り返し呼んでも同じ例外が再送出される
        with pytest.raises(CaptureFailure):
            hub.latest(timeout=2.0)
        with pytest.raises(CaptureFailure):
            source.capture()

        # キャプチャスレッドは終了し running が False になる
        _wait_until(lambda: not hub.running)
        hub.stop()

    def test_restart_clears_held_error(self):
        hub = FrameHub(FlakyCamera())
        hub.start()
        with pytest.raises(CaptureFailure):
            hub.latest(timeout=2.0)
        _wait_until(lambda: not hub.running)

        hub.start()
        try:
            assert isinstance(hub.latest(timeout=5.0), Image)
        finally:
            hub.stop()
