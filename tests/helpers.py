import subprocess
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from functools import wraps
from pathlib import Path
from typing import Literal, ParamSpec, TypeVar, override

import picamera2
import pytest

from pcbasm.config import Audio
from pcbasm.hal import Camera, CameraInfo, Resolution
from pcbasm.hal.audio import AudioPlayer
from pcbasm.vision import Image

PROJECT_ROOT = Path(__file__).parent.parent

TESTING_DATA_DIR = PROJECT_ROOT / "data" / "testing"

mark_hardware = pytest.mark.hardware

_P = ParamSpec("_P")
_R = TypeVar("_R")


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.02,
) -> None:
    """条件が成立するまでポーリングする（タイミングのアサートはしない）.

    成立しないまま timeout を超えたら pytest.fail する。sleep 固定値依存の アサートを避けるための共有ポーラ。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    pytest.fail(f"{timeout}s 以内に条件が成立しませんでした")


def _usb_camera_available() -> bool:
    """USBカメラ（uvcvideoドライバー）が接続されているか確認する."""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
        )
        return "uvcvideo" in result.stdout
    except Exception:
        return False


def _csi_camera_available() -> bool:
    """CSIカメラが接続されているか確認する."""
    return bool(picamera2.Picamera2.global_camera_info())


def _alsa_audio_available() -> bool:
    """Aplay が利用でき、ALSA に1台以上の再生デバイスが見えているか確認する."""
    try:
        result = subprocess.run(
            ["aplay", "-l"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return result.returncode == 0 and "card " in result.stdout.lower()
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _skip_if_hardware_unavailable(
    is_available: Callable[[], bool],
    reason: str,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """実行時にハードウェア接続を確認してテストをskipするdecoratorを返す."""

    def decorator(test: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(test)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            if not is_available():
                pytest.skip(reason)
            return test(*args, **kwargs)

        return wrapper

    return decorator


skip_if_no_usb_camera = _skip_if_hardware_unavailable(
    _usb_camera_available,
    "USBカメラが接続されていません",
)

skip_if_no_csi_camera = _skip_if_hardware_unavailable(
    _csi_camera_available,
    "CSIカメラが接続されていません",
)

skip_if_no_alsa_audio = _skip_if_hardware_unavailable(
    _alsa_audio_available,
    "ALSA再生デバイスが接続されていません",
)


class FakeAudioPlayer(AudioPlayer):
    """AudioPlayer 利用側を実ALSAなしで結合検証するテスト用実装."""

    def __init__(self, playback_error: Exception | None = None) -> None:
        self._playback_error = playback_error
        self._played: list[tuple[Literal["success", "failure"], Audio]] = []
        self._close_calls = 0

    @property
    def played(self) -> tuple[tuple[Literal["success", "failure"], Audio], ...]:
        return tuple(self._played)

    @property
    def close_calls(self) -> int:
        return self._close_calls

    @override
    def play(
        self,
        sound: Literal["success", "failure"],
        config: Audio,
    ) -> Future[None]:
        self._played.append((sound, config))
        future: Future[None] = Future()
        if self._playback_error is None:
            future.set_result(None)
        else:
            future.set_exception(self._playback_error)
        return future

    @override
    def close(self) -> None:
        self._close_calls += 1


class FakeCamera(Camera):
    """固定 Image 列を順に返すテスト用の Camera 実装.

    capture() のたびに与えられた画像を先頭から順に返し、 列を使い切った後は最後の画像を返し続ける。
    """

    def __init__(self, images: Sequence[Image], fps: float = 30.0) -> None:
        if not images:
            raise ValueError("imagesは1枚以上必要です")
        self._images = list(images)
        self._fps = fps
        self._index = 0

    @property
    @override
    def resolution(self) -> Resolution:
        first = self._images[0]
        return Resolution(width=first.width, height=first.height, fps=self._fps)

    @property
    @override
    def info(self) -> CameraInfo:
        return CameraInfo(name="FakeCamera", formats={"BGR": [self.resolution]})

    @override
    def capture(self) -> Image:
        image = self._images[min(self._index, len(self._images) - 1)]
        self._index += 1
        return image
