"""Toolhead_offset モジュールの仕様テスト.

- ToolheadOffsetResult: 計測結果の構築・roundtrip
- validate_offset_correction / locate_paste_blob: 計画書
  memory/agents/implementation-planner/purge-toolhead-calibration.md
  「公開 IF」節が契約（初回パージ痕によるオフセット自動キャリブレーション）

locate_paste_blob は実 OpenCV で描いた合成ブロブ画像 + FakeCamera
（tests/helpers.py）で検証する。klipper / stage は自前 HAL のため
mocker.Mock を使用（tests/pcbasm/posctrl/test_position.py と同パターン）。
cv2 / time.sleep 等の 3rd-party 表面はモックしない。
"""

from datetime import datetime

import cv2
import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcbasm import gcode
from pcbasm.geometry import Identity, Point2d, Point3d
from pcbasm.pasting import (
    ToolheadOffsetResult,
    locate_paste_blob,
    validate_offset_correction,
)
from pcbasm.vision import CalibrationResult, Image
from tests.helpers import FakeCamera


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


class TestValidateOffsetCorrection:
    """validate_offset_correction（計画書「公開 IF」節が契約）.

    (measured - current).norm > max_correction のとき日本語エラー文（差の値と
    閾値を含める）、それ以外（境界値ちょうどを含む）は None。
    """

    @pytest.mark.parametrize(
        ("measured", "current"),
        [
            # 差ゼロ
            (Point2d(1.0, 2.0), Point2d(1.0, 2.0)),
            # 差 (0.3, -0.4) → norm 0.5 < 1.0
            (Point2d(1.3, 1.6), Point2d(1.0, 2.0)),
            # 境界値ちょうど: 差 (1.0, 0.0) → norm == max_correction
            (Point2d(2.0, 2.0), Point2d(1.0, 2.0)),
            # 境界値ちょうど: 差 (0.0, -1.0) → norm == max_correction
            (Point2d(1.0, 1.0), Point2d(1.0, 2.0)),
        ],
    )
    def test_within_or_on_limit_returns_none(self, measured: Point2d, current: Point2d):
        assert validate_offset_correction(measured, current, 1.0) is None

    @pytest.mark.parametrize(
        ("measured", "current"),
        [
            # X 超過: 差 (1.5, 0.0)
            (Point2d(2.5, 2.0), Point2d(1.0, 2.0)),
            # Y 超過: 差 (0.0, -1.2)
            (Point2d(1.0, 0.8), Point2d(1.0, 2.0)),
            # 対角超過: 差 (0.9, 1.2) → norm 1.5
            (Point2d(1.9, 3.2), Point2d(1.0, 2.0)),
        ],
    )
    def test_exceeding_limit_returns_error_message(
        self, measured: Point2d, current: Point2d
    ):
        message = validate_offset_correction(measured, current, 1.0)

        assert message is not None

    def test_error_message_contains_difference_and_limit(self):
        """エラー文には差の大きさと閾値が含まれる（計画書: 差の値と閾値を含める）."""
        # 差 (0.9, 1.2) → norm 1.5、閾値 0.75
        message = validate_offset_correction(Point2d(1.9, 3.2), Point2d(1.0, 2.0), 0.75)

        assert message is not None
        assert "1.5" in message
        assert "0.75" in message


def _blob_image(center: tuple[int, int]) -> Image:
    """白背景 200x200 に直径 30px（10 px/mm で 3.0mm）の黒ブロブを描く."""
    array = np.full((200, 200, 3), 255, dtype=np.uint8)
    cv2.circle(array, center, 15, (0, 0, 0), -1)
    return Image(array)


def _blank_image() -> Image:
    return Image(np.full((200, 200, 3), 255, dtype=np.uint8))


def _calibration() -> CalibrationResult:
    return CalibrationResult(
        pixel_per_mm=10.0,
        square_size_mm=1.0,
        mean_distance_px=10.0,
        std_distance_px=0.1,
        resolution=(200, 200),
        crop_size=(200, 200),
        calibrated_at=datetime(2026, 7, 10, 12, 0, 0),
        z_position=12.0,
    )


class TestLocatePasteBlob:
    """locate_paste_blob（計画書「公開 IF」節が契約）.

    カメラをパージ痕へ移動（camera_position, z=calibration.z_position）して
    円検出し、収束後の最終カメラ位置（XYPositionAdjustor.adjust の戻り値 = ステージ位置 −
    観測オフセット）を返す。検出失敗は RuntimeError。
    """

    @pytest.fixture
    def klipper(self, mocker: MockerFixture):
        return mocker.Mock()

    @pytest.fixture
    def stage(self, mocker: MockerFixture):
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        stage.get_position.return_value = Point3d(10.0, 20.0, 5.0)
        stage.move.return_value = gcode.GCode("G1")
        return stage

    def _locate(self, camera: FakeCamera, klipper, stage, *, tolerance: float):
        return locate_paste_blob(
            camera=camera,
            klipper=klipper,
            stage=stage,
            calibration=_calibration(),
            offset_transform=Identity(),
            crop_size=(200, 200),
            camera_position=Point2d(50.0, 60.0),
            diameter_min=2.0,
            diameter_max=4.0,
            tolerance=tolerance,
            settle_time=0.0,
        )

    def test_centered_blob_converges_to_current_stage_position(self, klipper, stage):
        """画像中心のブロブ → オフセットほぼ 0 → ステージ現在位置を返す."""
        camera = FakeCamera([_blob_image((100, 100))])

        result = self._locate(camera, klipper, stage, tolerance=0.5)

        assert result.x == pytest.approx(10.0, abs=0.3)
        assert result.y == pytest.approx(20.0, abs=0.3)

    def test_moves_camera_to_purge_point_at_calibration_z_first(self, klipper, stage):
        """手順 1: camera_position + calibration.z_position へ移動して送信する."""
        camera = FakeCamera([_blob_image((100, 100))])

        self._locate(camera, klipper, stage, tolerance=0.5)

        first_move = stage.move.call_args_list[0].kwargs
        assert first_move["x"] == pytest.approx(50.0)
        assert first_move["y"] == pytest.approx(60.0)
        assert first_move["z"] == pytest.approx(12.0)
        # 中心一致 → 補正移動なし。移動はカメラ移動の 1 回だけ
        assert stage.move.call_count == 1
        assert klipper.send_gcode.call_count == 1

    def test_offset_blob_returns_position_corrected_by_observed_offset(
        self, klipper, stage
    ):
        """(2, 3)mm ずれたブロブ → 戻り値 = ステージ位置 − 観測オフセット.

        10 px/mm で (20, 30)px ずれ = (2.0, 3.0)mm。tolerance=5.0 で即収束し、
        (10−2, 20−3) = (8, 17) を返す（符号ピン）。
        """
        camera = FakeCamera([_blob_image((120, 130))])

        result = self._locate(camera, klipper, stage, tolerance=5.0)

        assert result.x == pytest.approx(8.0, abs=0.4)
        assert result.y == pytest.approx(17.0, abs=0.4)

    def test_blank_image_raises_runtime_error(self, klipper, stage):
        """ブロブなし画像 → 検出失敗の RuntimeError（パージ不良 = ジョブ中止）."""
        camera = FakeCamera([_blank_image()])

        with pytest.raises(RuntimeError) as exc:
            self._locate(camera, klipper, stage, tolerance=0.5)

        assert "失敗" in str(exc.value)

    def test_frame_sink_receives_annotated_frame_on_success(self, klipper, stage):
        """frame_sink が OffsetObserver へ配線され、検出成功時に 1 枚届く."""
        camera = FakeCamera([_blob_image((100, 100))])
        frames: list[Image] = []

        locate_paste_blob(
            camera=camera,
            klipper=klipper,
            stage=stage,
            calibration=_calibration(),
            offset_transform=Identity(),
            crop_size=(200, 200),
            camera_position=Point2d(50.0, 60.0),
            diameter_min=2.0,
            diameter_max=4.0,
            tolerance=0.5,
            frame_sink=frames.append,
            settle_time=0.0,
        )

        assert len(frames) == 1
