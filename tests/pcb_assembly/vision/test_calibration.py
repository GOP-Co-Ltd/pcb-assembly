from datetime import datetime
from pathlib import Path

import pytest

from pcb_assembly.vision.calibration import CalibrationResult


class TestCalibrationResult:
    """CalibrationResultクラスのテスト."""

    @pytest.fixture
    def sample(self) -> CalibrationResult:
        return CalibrationResult(
            pixel_per_mm=100.0,
            square_size_mm=1.5,
            mean_distance_px=150.0,
            std_distance_px=2.5,
            resolution=(640, 480),
            crop_size=(400, 400),
            calibrated_at=datetime(2025, 1, 6, 12, 0, 0),
        )

    def test_mm_per_pixel_returns_inverse(self, sample: CalibrationResult):
        assert sample.mm_per_pixel == pytest.approx(0.01)

    def test_to_dict_converts_all_fields(self, sample: CalibrationResult):
        data = sample.to_dict()

        assert data["pixel_per_mm"] == 100.0
        assert data["square_size_mm"] == 1.5
        assert data["resolution"] == (640, 480)
        assert data["crop_size"] == (400, 400)
        assert data["calibrated_at"] == "2025-01-06T12:00:00"

    def test_from_dict_restores_instance(self, sample: CalibrationResult):
        data = sample.to_dict()
        restored = CalibrationResult.from_dict(data)

        assert restored == sample

    def test_save_and_load_roundtrip(self, sample: CalibrationResult, tmp_path: Path):
        json_path = tmp_path / "calibration.json"
        sample.save(json_path)
        loaded = CalibrationResult.load(json_path)

        assert loaded == sample
