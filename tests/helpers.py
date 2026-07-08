import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import override

import picamera2
import pytest

from pcbasm.hal import Camera, CameraInfo, Resolution
from pcbasm.vision import Image

PROJECT_ROOT = Path(__file__).parent.parent

TESTING_DATA_DIR = PROJECT_ROOT / "data" / "testing"

mark_hardware = pytest.mark.hardware


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


skip_if_no_usb_camera = pytest.mark.skipif(
    not _usb_camera_available(),
    reason="USBカメラが接続されていません",
)

skip_if_no_csi_camera = pytest.mark.skipif(
    not picamera2.Picamera2.global_camera_info(),
    reason="CSIカメラが接続されていません",
)


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
