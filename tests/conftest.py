import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcb_assembly.hal.camera import CameraInfo, Resolution


@pytest.fixture
def mock_probe_backend(mocker: MockerFixture):
    """Probeクラスの内部実装をモックするフィクスチャ."""
    mock_encoder = mocker.MagicMock()
    mock_encoder.steps = 0
    mocker.patch(
        "pcb_assembly.hal.probe.RotaryEncoder",
        return_value=mock_encoder,
    )
    return mock_encoder


@pytest.fixture
def mock_camera_backend(mocker: MockerFixture):
    """Cameraクラス（USBバックエンド）の内部実装をモックするフィクスチャ."""
    mock_cam = mocker.MagicMock()
    mock_cam.isOpened.return_value = True
    mock_cam.set.return_value = True
    mock_cam.read.return_value = (True, np.zeros((720, 1280, 3), dtype=np.uint8))
    mocker.patch("cv2.VideoCapture", return_value=mock_cam)
    mocker.patch("pcb_assembly.hal.camera._UsbCamera._validate_device_id")

    # get_camera_infoをモックして、デフォルトの解像度をサポートするカメラ情報を返す
    default_info = CameraInfo(
        name="Mock Camera",
        formats={
            "MJPG": [
                Resolution(640, 480, 30.0),
                Resolution(1280, 720, 30.0),
            ]
        },
    )
    mocker.patch(
        "pcb_assembly.hal.camera.get_camera_info",
        return_value=default_info,
    )

    return mock_cam


@pytest.fixture
def mock_csi_camera_backend(mocker: MockerFixture):
    """Cameraクラス（CSIバックエンド）の内部実装をモックするフィクスチャ."""
    mock_picamera2 = mocker.MagicMock()
    mock_picamera2.Picamera2.global_camera_info.return_value = [
        {"Model": "Mock CSI Camera"}
    ]
    mock_picamera2.Picamera2.return_value.capture_array.return_value = np.zeros(
        (720, 1280, 3), dtype=np.uint8
    )
    mocker.patch("pcb_assembly.hal.camera.picamera2", mock_picamera2)
    return mock_picamera2
