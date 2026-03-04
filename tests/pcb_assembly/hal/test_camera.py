import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcb_assembly.hal.camera import (
    Camera,
    CameraInfo,
    Resolution,
    create_camera,
    get_camera_info,
)
from tests.helpers import mark_hardware, skip_if_no_csi_camera, skip_if_no_usb_camera


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
    @skip_if_no_usb_camera
    def test_returns_camera_info(self):
        info = get_camera_info(device_id=0)

        assert isinstance(info, CameraInfo)
        assert info.name != ""
        assert len(info.formats) > 0


class TestUsbCamera:
    """create_camera（USBバックエンド）のテスト."""

    def test_init_raises_when_camera_not_opened(self, mock_camera_backend):
        mock_camera_backend.isOpened.return_value = False

        with pytest.raises(RuntimeError, match="カメラ 0 を開けません"):
            create_camera(device_id=0, backend="usb")

    def test_init_raises_when_no_formats_available(
        self, mock_camera_backend, mocker: MockerFixture
    ):
        mocker.patch(
            "pcb_assembly.hal.camera.get_camera_info",
            return_value=CameraInfo(name="Empty Camera", formats={}),
        )

        with pytest.raises(RuntimeError, match="フォーマットがありません"):
            create_camera(backend="usb")

    def test_init_raises_when_format_length_invalid(self, mock_camera_backend):
        with pytest.raises(ValueError, match="4文字である必要があります"):
            create_camera(format="MJ", backend="usb")

    def test_init_raises_when_resolution_not_supported(self, mock_camera_backend):
        with pytest.raises(RuntimeError, match="サポートしていません"):
            create_camera(width=1920, height=1080, backend="usb")

    def test_init_warns_when_property_set_fails(self, mock_camera_backend):
        mock_camera_backend.set.return_value = False

        with pytest.warns(RuntimeWarning):
            create_camera(backend="usb")

    def test_capture_returns_image(self, mock_camera_backend):
        dummy_image = np.zeros((720, 1280, 3), dtype=np.uint8)
        mock_camera_backend.read.return_value = (True, dummy_image)

        camera = create_camera(backend="usb")
        image = camera.capture()

        assert image.width == 1280
        assert image.height == 720
        assert image.numpy().shape == (720, 1280, 3)
        assert image.numpy().dtype == np.uint8

    def test_capture_raises_on_failure(self, mock_camera_backend):
        mock_camera_backend.read.return_value = (False, None)

        camera = create_camera(backend="usb")

        with pytest.raises(RuntimeError, match="フレームの取得に失敗しました"):
            camera.capture()

    def test_capture_resizes_image_when_size_differs(self, mock_camera_backend):
        wrong_size_image = np.zeros((720, 1280, 3), dtype=np.uint8)
        mock_camera_backend.read.return_value = (True, wrong_size_image)

        camera = create_camera(width=640, height=480, backend="usb")

        with pytest.warns(RuntimeWarning, match="リサイズします"):
            image = camera.capture()

        assert image.width == 640
        assert image.height == 480

    def test_capture_converts_grayscale_to_bgr(self, mock_camera_backend):
        grayscale_image = np.zeros((720, 1280), dtype=np.uint8)
        mock_camera_backend.read.return_value = (True, grayscale_image)

        camera = create_camera(backend="usb")
        image = camera.capture()

        assert image.numpy().shape == (720, 1280, 3)

    def test_returns_camera_instance(self, mock_camera_backend):
        camera = create_camera(backend="usb")

        assert isinstance(camera, Camera)

    @mark_hardware
    @skip_if_no_usb_camera
    @pytest.mark.parametrize(
        ("width", "height"),
        [
            (640, 480),
            (1280, 720),
        ],
    )
    def test_capture_returns_valid_image_from_hardware(self, width, height):
        camera = create_camera(device_id=0, width=width, height=height)
        image = camera.capture()

        assert image.width == width
        assert image.height == height
        assert image.numpy().dtype == np.uint8


class TestCsiCamera:
    """create_camera（CSIバックエンド）のテスト."""

    def test_init_raises_when_camera_not_found(self, mock_csi_camera_backend):
        mock_csi_camera_backend.Picamera2.global_camera_info.return_value = []

        with pytest.raises(OSError, match="CSIカメラ 0 が見つかりません"):
            create_camera(backend="csi")

    def test_capture_returns_image(self, mock_csi_camera_backend):
        camera = create_camera(backend="csi")
        image = camera.capture()

        assert image.width == 1280
        assert image.height == 720
        assert image.numpy().shape == (720, 1280, 3)

    def test_capture_raises_on_failure(self, mock_csi_camera_backend):
        mock_csi_camera_backend.Picamera2.return_value.capture_array.side_effect = (
            RuntimeError("キャプチャ失敗")
        )

        camera = create_camera(backend="csi")

        with pytest.raises(RuntimeError):
            camera.capture()

    def test_returns_camera_instance(self, mock_csi_camera_backend):
        camera = create_camera(backend="csi")

        assert isinstance(camera, Camera)

    @mark_hardware
    @skip_if_no_csi_camera
    @pytest.mark.parametrize(
        ("width", "height"),
        [
            (640, 480),
            (1280, 720),
        ],
    )
    def test_capture_returns_valid_image_from_hardware(self, width, height):
        camera = create_camera(backend="csi", width=width, height=height)
        image = camera.capture()

        assert image.width == width
        assert image.height == height
        assert image.numpy().dtype == np.uint8


class TestCreateCamera:
    """create_camera ファクトリ関数のテスト."""

    def test_raises_when_backend_unknown(self, mock_camera_backend):
        with pytest.raises(ValueError, match="未知のバックエンド"):
            create_camera(backend="unknown")
