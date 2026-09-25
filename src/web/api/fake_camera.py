"""開発・E2E 用の固定画像カメラ."""

from __future__ import annotations

import time
from pathlib import Path
from typing import override

from pcbasm.hal import Camera, CameraInfo, Resolution
from pcbasm.vision import Image


class FixedImageCamera(Camera):
    """固定画像を返す開発・E2E 用カメラ。capture() は fps に合わせて待機する.

    fps ペーシングが無いと、FrameHub の専有スレッドが空回りして CPU を食う。

    そこで monotonic デッドライン方式で 1/fps 間隔に揃える。
    """

    def __init__(self, image_path: Path, fps: float = 15.0) -> None:
        """画像を 1 度だけ読み込み、以後 capture() は同一インスタンスを返す.

        Raises:
            FileNotFoundError: 画像が読み込めない場合（Image.load 由来）
        """
        self._image = Image.load(image_path)
        self._fps = fps
        self._next_deadline = time.monotonic()

    @property
    @override
    def resolution(self) -> Resolution:
        return Resolution(self._image.width, self._image.height, self._fps)

    @property
    @override
    def info(self) -> CameraInfo:
        return CameraInfo(name="FixedImageCamera", formats={"BGR": [self.resolution]})

    @override
    def capture(self) -> Image:
        now = time.monotonic()
        if now < self._next_deadline:
            time.sleep(self._next_deadline - now)
        self._next_deadline = max(
            self._next_deadline + 1.0 / self._fps, time.monotonic()
        )
        return self._image
