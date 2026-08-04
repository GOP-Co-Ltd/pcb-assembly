"""`web.api.fake_camera.FixedImageCamera` の仕様テスト.

計画書「`src/webui/fake_camera.py`」節が契約:

- 固定画像を 1 度だけロードし、capture() は同一 Image インスタンスを返し続ける
- resolution は画像サイズ + fps、info は name="FixedImageCamera"
- 不存在パスは FileNotFoundError（Image.load 由来）
- fps ペーシングのタイミングはアサートしない（実時間依存のフレーキー回避）
"""

from pathlib import Path

import pytest

from web.api.fake_camera import FixedImageCamera

from .conftest import FAKE_CAMERA_IMAGE


class TestFixedImageCamera:
    """固定画像カメラ（開発・E2E 用）."""

    def test_capture_returns_asset_sized_image(self):
        camera = FixedImageCamera(FAKE_CAMERA_IMAGE, fps=60.0)

        assert camera.capture().size == (1280, 720)

    def test_capture_returns_same_image_instance(self):
        camera = FixedImageCamera(FAKE_CAMERA_IMAGE, fps=60.0)

        assert camera.capture() is camera.capture()

    def test_resolution_and_info_reflect_image_and_fps(self):
        camera = FixedImageCamera(FAKE_CAMERA_IMAGE, fps=15.0)

        assert camera.resolution.size == (1280, 720)
        assert camera.resolution.fps == 15.0
        assert camera.info.name == "FixedImageCamera"

    def test_missing_image_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            FixedImageCamera(tmp_path / "missing.png")
