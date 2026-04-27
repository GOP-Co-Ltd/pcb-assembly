"""PasteApplicator のテスト."""

import pytest
from pytest_mock import MockerFixture
from shapely import box

from pcb_assembly import gcode
from pcb_assembly.control.pasting import PasteApplicator


@pytest.fixture
def mock_klipper(mocker: MockerFixture):
    return mocker.Mock()


@pytest.fixture
def mock_paste_dispenser(mocker: MockerFixture):
    dispenser = mocker.Mock()
    dispenser.enable.return_value = gcode.GCode()
    dispenser.disable.return_value = gcode.GCode()
    dispenser.pushpull.return_value = gcode.GCode()
    return dispenser


@pytest.fixture
def mock_stage(mocker: MockerFixture):
    stage = mocker.Mock()
    stage.max_velocity = 100.0
    stage.to_gcode.return_value = gcode.GCode()
    return stage


@pytest.fixture
def applicator(mock_klipper, mock_paste_dispenser, mock_stage):
    return PasteApplicator(
        klipper=mock_klipper,
        paste_dispenser=mock_paste_dispenser,
        stage=mock_stage,
        nozzle_size="23G",
        dispense_rate=5.0,
        dispense_accel=10.0,
        ul_per_mm2=0.05,
        retraction=10.0,
        retraction_rate=10.0,
        retraction_accel_factor=2.0,
        paste_height=0.5,
        lift_height=5.0,
    )


class TestPasteApplicator:
    def test_apply_calls_send_gcode_per_polygon(self, applicator, mock_klipper):
        """apply()がポリゴン数ぶんだけklipper.send_gcodeを呼ぶ."""
        polygons = [box(0, 0, 2, 3), box(5, 5, 8, 9)]
        applicator.apply(polygons)
        assert mock_klipper.send_gcode.call_count == 2

    def test_apply_dispense_uses_sync_false(self, applicator, mock_paste_dispenser):
        """プライム+吐出がsync=Falseで呼ばれることを確認する."""
        polygon = box(0, 0, 2, 3)
        applicator.apply([polygon])

        calls = mock_paste_dispenser.pushpull.call_args_list
        # プライム+吐出（sync=False）、リトラクション（sync=True）の2回
        assert len(calls) == 2
        # 1回目: プライム+吐出の連続動作
        assert calls[0].kwargs.get("sync") is False
        # 2回目: リトラクション（デフォルトsync=True）
        assert "sync" not in calls[1].kwargs or calls[1].kwargs["sync"] is True

    def test_apply_combined_prime_and_dispense_amount(
        self, applicator, mock_paste_dispenser
    ):
        """プライム+吐出量が retraction + area * ul_per_mm2 であることを確認する."""
        polygon = box(0, 0, 2, 3)  # area = 6 mm²
        retraction = 10.0
        ul_per_mm2 = 0.05
        expected_amount = retraction + 6.0 * ul_per_mm2

        applicator.apply([polygon])

        calls = mock_paste_dispenser.pushpull.call_args_list
        assert calls[0].args[0] == pytest.approx(expected_amount)

    def test_apply_empty_polygons(self, applicator, mock_klipper):
        """空のポリゴンリストではsend_gcodeが呼ばれない."""
        applicator.apply([])
        mock_klipper.send_gcode.assert_not_called()

    def test_context_manager(self, applicator, mock_klipper, mock_paste_dispenser):
        """コンテキストマネージャでenable/disableが呼ばれる."""
        with applicator:
            pass
        mock_paste_dispenser.enable.assert_called_once()
        mock_paste_dispenser.disable.assert_called_once()

    def test_retraction_accel_factor_validation(
        self, mock_klipper, mock_paste_dispenser, mock_stage
    ):
        """retraction_accel_factor <= 1.0 で ValueError."""
        with pytest.raises(ValueError, match="retraction_accel_factor"):
            PasteApplicator(
                klipper=mock_klipper,
                paste_dispenser=mock_paste_dispenser,
                stage=mock_stage,
                nozzle_size="23G",
                dispense_rate=5.0,
                dispense_accel=1.0,
                ul_per_mm2=0.05,
                retraction=10.0,
                retraction_rate=10.0,
                retraction_accel_factor=0.5,
            )

    def test_invalid_nozzle_size(self, mock_klipper, mock_paste_dispenser, mock_stage):
        """無効なnozzle_sizeでValueError."""
        with pytest.raises(ValueError, match="無効なnozzle_size"):
            PasteApplicator(
                klipper=mock_klipper,
                paste_dispenser=mock_paste_dispenser,
                stage=mock_stage,
                nozzle_size="99G",
                dispense_rate=5.0,
                dispense_accel=1.0,
                ul_per_mm2=0.05,
                retraction=10.0,
                retraction_rate=10.0,
                retraction_accel_factor=2.0,
            )
