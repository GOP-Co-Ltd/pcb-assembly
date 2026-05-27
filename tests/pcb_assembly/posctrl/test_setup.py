"""Control/setup モジュールのテスト."""

import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcb_assembly.geometry import Point2d
from pcb_assembly.posctrl.setup import OffsetObserver, machine_session
from pcb_assembly.vision import Image
from pcb_assembly.vision.detection import OffsetStatistics


class TestOffsetObserver:
    """OffsetObserverのテスト."""

    @pytest.fixture
    def mock_camera(self, mocker: MockerFixture):
        camera = mocker.Mock()
        dummy_image = Image(np.zeros((720, 1280, 3), dtype=np.uint8))
        camera.capture.return_value = dummy_image
        return camera

    @pytest.fixture
    def mock_detector(self, mocker: MockerFixture):
        detector = mocker.Mock()
        return detector

    @pytest.fixture
    def mock_cv2(self, mocker: MockerFixture):
        mocker.patch("pcb_assembly.posctrl.setup.cv2")

    def test_returns_mean_mm_on_success(
        self,
        mock_detector,
        mock_camera,
        mock_cv2,
    ):
        """検出成功時にmean_mmを返すことを確認."""
        expected_offset = Point2d(0.5, -0.3)
        mock_detector.detect_with_statistics.return_value = OffsetStatistics(
            mean=Point2d(50.0, -30.0),
            std=Point2d(1.0, 1.0),
            pixel_per_mm=100.0,
            sample_count=30,
        )

        observer = OffsetObserver(
            detector=mock_detector,
            camera=mock_camera,
            crop_size=(200, 200),
            window_name="test",
            sample_count=10,
        )

        result = observer()

        assert result.x == pytest.approx(expected_offset.x)
        assert result.y == pytest.approx(expected_offset.y)

    def test_raises_on_detection_failure(
        self,
        mock_detector,
        mock_camera,
        mock_cv2,
    ):
        """検出失敗時にRuntimeErrorを発生させることを確認."""
        mock_detector.detect_with_statistics.return_value = None

        observer = OffsetObserver(
            detector=mock_detector,
            camera=mock_camera,
            crop_size=(200, 200),
            window_name="test",
        )

        with pytest.raises(RuntimeError, match="検出に失敗しました"):
            observer()


class TestMachineSession:
    """machine_sessionのテスト."""

    def test_sends_m84_and_destroys_windows(self, mocker: MockerFixture):
        """セッション終了時にM84送信とcv2.destroyAllWindowsが呼ばれることを確認."""
        mock_klipper = mocker.Mock()
        mock_destroy = mocker.patch("pcb_assembly.posctrl.setup.cv2.destroyAllWindows")

        with machine_session(mock_klipper):
            pass

        mock_klipper.send_gcode.assert_called_once_with("M84")
        mock_destroy.assert_called_once()

    def test_cleanup_on_exception(self, mocker: MockerFixture):
        """例外発生時でもクリーンアップが実行されることを確認."""
        mock_klipper = mocker.Mock()
        mock_destroy = mocker.patch("pcb_assembly.posctrl.setup.cv2.destroyAllWindows")

        with pytest.raises(ValueError, match="test error"):
            with machine_session(mock_klipper):
                raise ValueError("test error")

        mock_klipper.send_gcode.assert_called_once_with("M84")
        mock_destroy.assert_called_once()
