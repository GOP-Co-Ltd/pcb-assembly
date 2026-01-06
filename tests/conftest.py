import pytest
from pytest_mock import MockerFixture


@pytest.fixture
def mock_video_capture(mocker: MockerFixture):
    """cv2.VideoCaptureをモックするフィクスチャ."""
    mock_cam = mocker.MagicMock()
    mock_cam.isOpened.return_value = True
    mock_cam.set.return_value = True
    mocker.patch("cv2.VideoCapture", return_value=mock_cam)
    return mock_cam
