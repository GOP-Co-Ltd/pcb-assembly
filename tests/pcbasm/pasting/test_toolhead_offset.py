"""ToolheadOffsetResult のテスト."""

from datetime import datetime

import pytest

from pcbasm.geometry import Point2d
from pcbasm.pasting import ToolheadOffsetResult


def _make_result() -> ToolheadOffsetResult:
    return ToolheadOffsetResult(
        offset=Point2d(x=1.5, y=-2.3),
        dispense_position=Point2d(x=100.0, y=200.0),
        camera_position=Point2d(x=98.5, y=202.3),
        tolerance=0.05,
        calibrated_at=datetime(2026, 3, 18, 12, 0, 0),
    )


class TestToolheadOffsetResult:
    def test_to_dict_from_dict_roundtrip(self):
        result = _make_result()
        data = result.to_dict()
        restored = ToolheadOffsetResult.from_dict(data)
        assert restored == result

    def test_save_load_roundtrip(self, tmp_path):
        result = _make_result()
        path = tmp_path / "offset.json"
        result.save(path)
        loaded = ToolheadOffsetResult.load(path)
        assert loaded == result

    def test_to_dict_contains_expected_keys(self):
        result = _make_result()
        data = result.to_dict()
        assert data["offset"] == {"x": 1.5, "y": -2.3}
        assert data["dispense_position"] == {"x": 100.0, "y": 200.0}
        assert data["camera_position"] == {"x": 98.5, "y": 202.3}
        assert data["tolerance"] == 0.05

    def test_saved_file_is_valid_json(self, tmp_path):
        import json

        result = _make_result()
        path = tmp_path / "offset.json"
        result.save(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "offset" in data


class TestMeasure:
    """Measure は吐出位置 − カメラ検出位置でオフセットを算出する。"""

    def test_offset_is_dispense_minus_camera(self):
        calibrated_at = datetime(2026, 3, 18, 12, 0, 0)

        result = ToolheadOffsetResult.measure(
            dispense_position=Point2d(x=100.0, y=200.0),
            camera_position=Point2d(x=98.5, y=202.3),
            tolerance=0.05,
            calibrated_at=calibrated_at,
        )

        assert result.offset.x == pytest.approx(1.5)
        assert result.offset.y == pytest.approx(-2.3)
        assert result.dispense_position == Point2d(x=100.0, y=200.0)
        assert result.camera_position == Point2d(x=98.5, y=202.3)
        assert result.tolerance == pytest.approx(0.05)
        assert result.calibrated_at == calibrated_at

    def test_measured_result_round_trips_like_manual_construction(self):
        measured = ToolheadOffsetResult.measure(
            dispense_position=Point2d(x=100.0, y=200.0),
            camera_position=Point2d(x=98.5, y=202.3),
            tolerance=0.05,
            calibrated_at=datetime(2026, 3, 18, 12, 0, 0),
        )

        assert ToolheadOffsetResult.from_dict(measured.to_dict()) == measured
