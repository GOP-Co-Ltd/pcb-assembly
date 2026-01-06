import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcb_assembly.hal.camera import (
    Camera,
    CameraInfo,
    Resolution,
    get_camera_info,
)
from tests.helpers import mark_hardware


class TestResolution:
    """Resolutionクラスのテスト."""

    def test_size_returns_width_height_tuple(self):
        resolution = Resolution(width=1280, height=720, fps=30.0)

        assert resolution.size == (1280, 720)


class TestCameraInfo:
    """CameraInfoクラスのテスト."""

    @pytest.mark.parametrize(
        ("format_name", "query_resolution", "expected"),
        [
            # フォーマットと解像度が一致する場合
            ("MJPG", Resolution(1280, 720, 30.0), True),
            # フォーマットが存在しない場合
            ("YUYV", Resolution(1280, 720, 30.0), False),
            # 解像度が存在しない場合
            ("MJPG", Resolution(1920, 1080, 30.0), False),
        ],
    )
    def test_has_format(self, format_name, query_resolution, expected):
        resolution_720p = Resolution(width=1280, height=720, fps=30.0)
        info = CameraInfo(name="Test Camera", formats={"MJPG": [resolution_720p]})

        assert info.has_format(format_name, query_resolution) is expected


class TestGetCameraInfo:
    """get_camera_info関数のテスト."""

    @mark_hardware
    def test_returns_camera_info(self):
        info = get_camera_info(device_id=0)

        assert isinstance(info, CameraInfo)
        assert info.name != ""
        assert len(info.formats) > 0


class TestCamera:
    """Cameraクラスのテスト."""

    def test_init_raises_when_camera_not_opened(self, mock_video_capture):
        mock_video_capture.isOpened.return_value = False

        with pytest.raises(RuntimeError, match="カメラ 0 を開けません"):
            Camera(device_id=0)

    def test_init_raises_when_no_formats_available(
        self, mock_video_capture, mocker: MockerFixture
    ):
        mocker.patch(
            "pcb_assembly.hal.camera.get_camera_info",
            return_value=CameraInfo(name="Empty Camera", formats={}),
        )

        with pytest.raises(RuntimeError, match="フォーマットがありません"):
            Camera()

    def test_init_raises_when_format_length_invalid(self, mock_video_capture):
        with pytest.raises(ValueError, match="4文字である必要があります"):
            Camera(format="MJ")

    def test_init_raises_when_resolution_not_supported(self, mock_video_capture):
        with pytest.raises(RuntimeError, match="サポートしていません"):
            Camera(width=1920, height=1080)

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
