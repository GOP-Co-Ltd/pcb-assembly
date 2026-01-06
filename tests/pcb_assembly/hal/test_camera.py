import numpy as np
import pytest

from pcb_assembly.hal.camera import Camera
from tests.helpers import mark_hardware


class TestCamera:
    """Cameraクラスのテスト."""

    def test_init_raises_when_camera_not_opened(self, mock_video_capture):
        mock_video_capture.isOpened.return_value = False

        with pytest.raises(RuntimeError, match="カメラ 0 を開けません"):
            Camera(device_id=0)

    def test_init_warns_when_property_set_fails(self, mock_video_capture):
        mock_video_capture.set.return_value = False

        with pytest.warns(RuntimeWarning):
            Camera()

    def test_capture_returns_image(self, mock_video_capture):
        dummy_image = np.zeros((480, 640, 3), dtype=np.uint8)
        mock_video_capture.read.return_value = (True, dummy_image)

        camera = Camera()
        image = camera.capture()

        assert image.shape == (480, 640, 3)
        assert image.dtype == np.uint8

    def test_capture_raises_on_failure(self, mock_video_capture):
        mock_video_capture.read.return_value = (False, None)

        camera = Camera()

        with pytest.raises(RuntimeError, match="フレームの取得に失敗しました"):
            camera.capture()

    def test_capture_resizes_image_when_size_differs(self, mock_video_capture):
        wrong_size_image = np.zeros((720, 1280, 3), dtype=np.uint8)
        mock_video_capture.read.return_value = (True, wrong_size_image)

        camera = Camera(width=640, height=480)

        with pytest.warns(RuntimeWarning, match="リサイズします"):
            image = camera.capture()

        assert image.shape == (480, 640, 3)

    def test_capture_converts_grayscale_to_bgr(self, mock_video_capture):
        grayscale_image = np.zeros((480, 640), dtype=np.uint8)
        mock_video_capture.read.return_value = (True, grayscale_image)

        camera = Camera()
        image = camera.capture()

        assert image.shape == (480, 640, 3)

    @mark_hardware
    @pytest.mark.parametrize(
        ("width", "height"),
        [
            (640, 480),
            (1280, 720),
        ],
    )
    def test_capture_returns_valid_image_from_hardware(self, width, height):
        camera = Camera(device_id=0, width=width, height=height)
        image = camera.capture()

        assert image.shape == (height, width, 3)
        assert image.dtype == np.uint8
