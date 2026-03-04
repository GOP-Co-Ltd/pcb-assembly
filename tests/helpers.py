import subprocess
from pathlib import Path

import picamera2
import pytest

PROJECT_ROOT = Path(__file__).parent.parent

TESTING_DATA_DIR = PROJECT_ROOT / "data" / "testing"

mark_hardware = pytest.mark.hardware


def _usb_camera_available() -> bool:
    """USBカメラ（uvcvideoドライバー）が接続されているか確認する."""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
        )
        return "uvcvideo" in result.stdout
    except Exception:
        return False


skip_if_no_usb_camera = pytest.mark.skipif(
    not _usb_camera_available(),
    reason="USBカメラが接続されていません",
)

skip_if_no_csi_camera = pytest.mark.skipif(
    not picamera2.Picamera2.global_camera_info(),
    reason="CSIカメラが接続されていません",
)
