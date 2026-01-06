import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcb_assembly.hal.camera import CameraInfo, Resolution


@pytest.fixture
def mock_video_capture(mocker: MockerFixture):
    """cv2.VideoCaptureをモックするフィクスチャ."""
    mock_cam = mocker.MagicMock()
    mock_cam.isOpened.return_value = True
    mock_cam.set.return_value = True
    mock_cam.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8))
    mocker.patch("cv2.VideoCapture", return_value=mock_cam)
    mocker.patch("pcb_assembly.hal.camera.Camera._validate_device_id")

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
