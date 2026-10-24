from datetime import datetime
from pathlib import Path

import attrs
import cv2
import numpy as np
import pytest

from pcbasm.vision import Image
from pcbasm.vision.calibration import (
    CameraCalibration,
    CheckerboardCalibrator,
    load_focus_z,
)
from tests.support.paths import TESTING_DATA_DIR


class TestCameraCalibration:
    """CameraCalibrationクラスのテスト."""

    @pytest.fixture
    def sample(self) -> CameraCalibration:
        return CameraCalibration(
            pixel_per_mm=100.0,
            square_size_mm=1.5,
            mean_distance_px=150.0,
            std_distance_px=2.5,
            resolution=(640, 480),
            crop_size=(400, 400),
            calibrated_at=datetime(2025, 1, 6, 12, 0, 0),
        )

    def test_mm_per_pixel_returns_inverse(self, sample: CameraCalibration):
        assert sample.mm_per_pixel == pytest.approx(0.01)

    def test_to_dict_converts_all_fields(self, sample: CameraCalibration):
        data = sample.to_dict()

        assert data["pixel_per_mm"] == 100.0
        assert data["square_size_mm"] == 1.5
        assert data["resolution"] == (640, 480)
        assert data["crop_size"] == (400, 400)
        assert data["calibrated_at"] == "2025-01-06T12:00:00"

    @pytest.mark.parametrize(
        "z_position", [None, 15.5], ids=["without-z-position", "with-z-position"]
    )
    def test_save_and_load_roundtrip(
        self, sample: CameraCalibration, tmp_path: Path, z_position: float | None
    ):
        result = attrs.evolve(sample, z_position=z_position)
        json_path = tmp_path / "calibration.json"
        result.save(json_path)

        loaded = CameraCalibration.load(json_path)

        assert loaded == result
        assert loaded.z_position == z_position

    def test_load_legacy_json_without_z_position(
        self, sample: CameraCalibration, tmp_path: Path
    ):
        """z_positionフィールドが無いJSONファイルも正常に読み込める."""
        json_path = tmp_path / "legacy.json"
        sample.save(json_path)
        import json

        data = json.loads(json_path.read_text())
        data.pop("z_position", None)
        json_path.write_text(json.dumps(data))

        loaded = CameraCalibration.load(json_path)
        assert loaded.z_position is None


class TestLoadFocusZ:
    """校正ファイルに記録した焦点 Z（/api/state の focus_z）."""

    def _save(self, path: Path, z_position: float | None) -> None:
        CameraCalibration(
            pixel_per_mm=100.0,
            square_size_mm=1.5,
            mean_distance_px=150.0,
            std_distance_px=2.5,
            resolution=(640, 480),
            crop_size=(400, 400),
            calibrated_at=datetime(2025, 1, 6, 12, 0, 0),
            z_position=z_position,
        ).save(path)

    def test_returns_recorded_z_position(self, tmp_path: Path):
        path = tmp_path / "calibration.json"
        self._save(path, -25.0)

        assert load_focus_z(path) == -25.0

    def test_is_none_before_calibration(self, tmp_path: Path):
        assert load_focus_z(tmp_path / "calibration.json") is None

    def test_is_none_when_z_position_was_not_recorded(self, tmp_path: Path):
        path = tmp_path / "calibration.json"
        self._save(path, None)

        assert load_focus_z(path) is None


class TestCheckerboardCalibrator:
    """CheckerboardCalibratorクラスのテスト."""

    @pytest.fixture
    def checkerboard_image(self) -> Image:
        """5x5内部コーナー、1マス約66.7pxのチェッカーボード画像(400x400)."""
        return Image.load(TESTING_DATA_DIR / "checkerboard.png")

    @pytest.fixture
    def calibrator(self) -> CheckerboardCalibrator:
        """標準的なキャリブレータ."""
        return CheckerboardCalibrator(
            square_size_mm=10.0,
            crop_size=(400, 400),
        )

    def test_calibrate_calculates_correct_pixel_per_mm(
        self, calibrator: CheckerboardCalibrator, checkerboard_image: Image
    ):
        result = calibrator.calibrate(checkerboard_image)
        assert result is not None
        calibration_result, _ = result

        # 400px / 6マス = 66.67px/マス、10mm/マスなので pixel_per_mm = 66.67 / 10 = 6.667
        assert calibration_result.pixel_per_mm == pytest.approx(400 / 6 / 10, rel=0.01)

    def test_calibrate_sets_correct_metadata(
        self, calibrator: CheckerboardCalibrator, checkerboard_image: Image
    ):
        result = calibrator.calibrate(checkerboard_image)
        assert result is not None
        calibration_result, _ = result

        assert calibration_result.square_size_mm == 10.0
        assert calibration_result.resolution == (400, 400)
        assert calibration_result.crop_size == (400, 400)

    def test_calibrate_returns_none_when_no_checkerboard(
        self, calibrator: CheckerboardCalibrator
    ):
        blank_image = Image(np.full((400, 400, 3), 128, dtype=np.uint8))

        result = calibrator.calibrate(blank_image)

        assert result is None
