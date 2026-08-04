import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcbasm.hal.camera import CameraInfo, Resolution


@pytest.fixture(autouse=True)
def disable_mdns_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Settings.from_env()` 経路の mDNS 広告・探索を全テストで無効にする.

    共有 fixture は `discovery_enabled=False` を明示的に渡しているが、env から Settings
    を組むテスト（`uvicorn --factory` と同じ経路の検証）はそれを通らない。 実 LAN
    へ広告・探索を漏らさないための最後の砦なので、mDNS を実際に使う テストは env ではなくコンストラクタ注入（ランダムなサービス型
    + `interfaces=["127.0.0.1"]`）で自分の設定を作る。
    """
    monkeypatch.setenv("PCBASM_API_DISCOVERY_ENABLED", "0")
    monkeypatch.setenv("PCBASM_UI_DISCOVERY_ENABLED", "0")


@pytest.fixture
def mock_camera_backend(mocker: MockerFixture):
    """Cameraクラス（USBバックエンド）の内部実装をモックするフィクスチャ."""
    mock_cam = mocker.MagicMock()
    mock_cam.isOpened.return_value = True
    mock_cam.set.return_value = True
    mock_cam.read.return_value = (True, np.zeros((720, 1280, 3), dtype=np.uint8))
    mocker.patch("cv2.VideoCapture", return_value=mock_cam)
    mocker.patch("pcbasm.hal.camera._UsbCamera._validate_device_id")

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
        "pcbasm.hal.camera.get_camera_info",
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
    mocker.patch("pcbasm.hal.camera.picamera2", mock_picamera2)
    return mock_picamera2
