"""カメラ専有スレッドで capture し、最新フレームを複数消費者へ共有する FrameHub."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import override

from pcbasm.hal.camera import Camera, CameraInfo, Resolution
from pcbasm.vision import Image

logger = logging.getLogger(__name__)

type _ReadyPredicate = Callable[[int], bool]


class FrameHub:
    """カメラ専有スレッドで capture し、最新フレームを複数消費者へ共有する.

    専有スレッド 1 本だけが ``camera.capture()`` を呼び、消費者は Condition を
    wait するだけでデバイスに触れない。``Image`` はイミュータブルなので
    最新フレームは参照共有する（コピーしない）。
    """

    def __init__(self, camera: Camera) -> None:
        self._camera = camera
        self._cond = threading.Condition()
        self._seq = 0  # 単調増加シーケンス番号（stop/start でリセットしない）
        self._frame: Image | None = None
        self._error: BaseException | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

    @property
    def running(self) -> bool:
        """キャプチャスレッドが稼働中か."""
        with self._cond:
            return self._running

    def start(self) -> None:
        """キャプチャスレッドを起動する（冪等）.

        stop 後に再び start できる。保持中のキャプチャエラーはクリアする。
        """
        with self._cond:
            if self._running:
                return
            self._error = None
            self._running = True
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._capture_loop,
                args=(self._stop_event,),
                name="FrameHub",
                daemon=True,
            )
            self._thread.start()
        logger.info("FrameHub started")

    def stop(self, timeout: float = 5.0) -> None:
        """キャプチャスレッドを停止して join する（冪等）。camera は閉じない.

        Args:
            timeout: join の待ち上限 [sec]。超過時は warning を出して戻る
        """
        with self._cond:
            thread = self._thread
            self._thread = None
            self._running = False
            if self._stop_event is not None:
                self._stop_event.set()
                self._stop_event = None
            self._cond.notify_all()
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("FrameHub thread did not stop within %.1fs", timeout)
            else:
                logger.info("FrameHub stopped")

    def latest(self, timeout: float = 5.0) -> Image:
        """最新フレームを返す。初回到着まで待つ.

        停止後でも最後のフレームが存在すれば待たずに返す。

        Raises:
            TimeoutError: timeout 以内にフレームが到着しない場合
            RuntimeError: 未 start・停止済みでフレームが 1 枚も無い場合
            BaseException: キャプチャスレッドの保持例外（再送出、繰り返し可）
        """
        with self._cond:
            frame, _ = self._wait_for(lambda seq: self._frame is not None, timeout)
            return frame

    def subscribe(self, timeout: float = 5.0) -> FrameSource:
        """消費者カーソル付きの FrameSource を返す.

        Args:
            timeout: ``FrameSource.capture()`` 1 回あたりの待ち上限 [sec]
        """
        return FrameSource(self._camera, self._wait_next, timeout)

    def _wait_next(self, cursor: int, timeout: float) -> tuple[Image, int]:
        """``cursor`` より新しいフレームを待って (フレーム, シーケンス番号) を返す."""
        with self._cond:
            return self._wait_for(lambda seq: seq > cursor, timeout)

    def _wait_for(self, ready: _ReadyPredicate, timeout: float) -> tuple[Image, int]:
        """``ready(seq)`` が真になるまで待ち、(フレーム, シーケンス番号) を返す.

        呼び出し側が ``self._cond`` を保持していること。
        """
        deadline: float | None = None
        while True:
            if self._error is not None:
                raise self._error
            if ready(self._seq) and self._frame is not None:
                return self._frame, self._seq
            if not self._running:
                raise RuntimeError(
                    "FrameHub は停止しています（新しいフレームは到着しません）"
                )
            now = time.monotonic()
            if deadline is None:
                deadline = now + timeout
            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError(f"フレーム待ちがタイムアウトしました: {timeout}s")
            self._cond.wait(remaining)

    def _capture_loop(self, stop_event: threading.Event) -> None:
        try:
            while not stop_event.is_set():
                frame = self._camera.capture()
                with self._cond:
                    self._seq += 1
                    self._frame = frame
                    self._cond.notify_all()
        except BaseException as exc:  # noqa: BLE001 — 消費者へ保持・再送出する
            with self._cond:
                self._error = exc
                self._running = False
                self._cond.notify_all()


class FrameSource(Camera):
    """``FrameHub.subscribe()`` が生成する Camera 実装。直接コンストラクトしない.

    消費者ごとのカーソルを持ち「呼ぶたびに新しいフレーム」を保証するため、
    ``CircleDetector.detect_with_statistics()`` など既存検出コードへ無改造で
    注入できる。
    """

    def __init__(
        self,
        camera: Camera,
        wait_next: Callable[[int, float], tuple[Image, int]],
        timeout: float,
    ) -> None:
        self._camera = camera
        self._wait_next = wait_next
        self._timeout = timeout
        self._cursor = 0

    @property
    @override
    def resolution(self) -> Resolution:
        return self._camera.resolution

    @property
    @override
    def info(self) -> CameraInfo:
        return self._camera.info

    @override
    def capture(self) -> Image:
        """自分のカーソルより新しいフレームが来るまで待ち、カーソルを進めて返す.

        Raises:
            TimeoutError: timeout 以内に新しいフレームが到着しない場合
            RuntimeError: 待機中に hub が停止した場合
            BaseException: キャプチャスレッドの保持例外（再送出）
        """
        frame, self._cursor = self._wait_next(self._cursor, self._timeout)
        return frame
