import numpy as np
import pytest
from pytest_mock import MockerFixture


@pytest.fixture
def mock_video_capture(mocker: MockerFixture):
    """cv2.VideoCaptureをモックするフィクスチャ."""
    mock_cam = mocker.MagicMock()
    mock_cam.isOpened.return_value = True
    mock_cam.set.return_value = True
    mock_cam.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8))
    mocker.patch("cv2.VideoCapture", return_value=mock_cam)
    mocker.patch("pcb_assembly.hal.camera.Camera._validate_device_id")
    return mock_cam
