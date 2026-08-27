import pytest
from pytest_mock import MockerFixture

from pcbasm.gcode import GCode
from pcbasm.hal.klipper import Klipper
from pcbasm.hal.paste_dispenser import (
    PasteDispenser,
)
from tests.helpers import mark_hardware

STEPPER_NAME = "paste_dispenser"
PREFIX = f"MANUAL_STEPPER STEPPER={STEPPER_NAME}"


class TestPasteDispenser:
    """PasteDispenserクラスのテスト."""

    @pytest.fixture
    def mock_klipper(self, mocker: MockerFixture):
        """Klipperをモックするフィクスチャ."""
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                f"manual_stepper {STEPPER_NAME}": {"rotation_distance": "0.5"},
                "output_pin air_pump": {},
            },
        )
        return klipper

    @mark_hardware
    def test_init(self):
        klipper = Klipper()
        PasteDispenser(klipper.readonly, rotations_per_ul=0.5)

    def test_init_missing_stepper_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={"output_pin air_pump": {}},
        )

        with pytest.raises(
            RuntimeError, match=r"printer\.cfgに\[manual_stepper paste_dispenser\]"
        ):
            PasteDispenser(klipper.readonly, rotations_per_ul=0.5)

    def test_init_missing_air_pump_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                f"manual_stepper {STEPPER_NAME}": {"rotation_distance": "0.5"},
            },
        )

        with pytest.raises(
            RuntimeError, match=r"printer\.cfgに\[output_pin air_pump\]"
        ):
            PasteDispenser(klipper.readonly, rotations_per_ul=0.5)

    def test_enable(self, mock_klipper: Klipper):
        dispenser = PasteDispenser(mock_klipper.readonly, rotations_per_ul=0.5)
        gcode = dispenser.enable()

        assert isinstance(gcode, GCode)
        lines = gcode.to_list()
        assert lines[0] == "SET_PIN PIN=air_pump VALUE=1"
        assert lines[1] == f"{PREFIX} ENABLE=1"

    def test_disable(self, mock_klipper: Klipper):
        dispenser = PasteDispenser(mock_klipper.readonly, rotations_per_ul=0.5)
        gcode = dispenser.disable()

        assert isinstance(gcode, GCode)
        lines = gcode.to_list()
        assert lines[0] == "SET_PIN PIN=air_pump VALUE=0"
        assert lines[1] == f"{PREFIX} ENABLE=0"

    @pytest.mark.parametrize(
        ("amount", "expected_move_sign"),
        [
            (1.0, ""),  # 正: 吐出
            (-1.0, "-"),  # 負: リトラクション
        ],
    )
    def test_pushpull(
        self, mock_klipper: Klipper, amount: float, expected_move_sign: str
    ):
        # rotations_per_ul=0.5, rotation_distance=0.5
        # 1μL → 0.5rev → 180deg → 180/360 * 0.5mm = 0.25mm
        rotations_per_ul = 0.5
        dispenser = PasteDispenser(
            mock_klipper.readonly, rotations_per_ul=rotations_per_ul
        )

        rate = 2.0  # μL/sec → 0.5*360*2 = 360 deg/s → 0.5mm/s
        accel = 4.0  # μL/sec² → 0.5*360*4 = 720 deg/s² → 1.0mm/s²

        gcode = dispenser.pushpull(amount, rate, accel)

        assert isinstance(gcode, GCode)
        lines = gcode.to_list()
        assert lines[0] == f"{PREFIX} SET_POSITION=0.0"
        assert lines[1] == f"{PREFIX} MOVE={expected_move_sign}0.25 SPEED=0.5 ACCEL=1.0"

    def test_pushpull_sync_false(self, mock_klipper: Klipper):
        dispenser = PasteDispenser(mock_klipper.readonly, rotations_per_ul=0.5)
        gcode = dispenser.pushpull(1.0, 2.0, 4.0, sync=False)

        lines = gcode.to_list()
        assert lines[1].endswith("SYNC=0")

    def test_continue_pushpull_keeps_current_coordinate(self, mock_klipper: Klipper):
        dispenser = PasteDispenser(mock_klipper.readonly, rotations_per_ul=0.5)

        gcode = dispenser.continue_pushpull(1.0, -0.4, 2.0, 4.0, sync=False)

        assert gcode.to_list() == [f"{PREFIX} MOVE=0.15 SPEED=0.5 ACCEL=1.0 SYNC=0"]

    def test_sync(self, mock_klipper: Klipper):
        dispenser = PasteDispenser(mock_klipper.readonly, rotations_per_ul=0.5)

        assert dispenser.sync().to_list() == [f"{PREFIX} SYNC=1"]

    def test_rotate_revolutions(self, mock_klipper: Klipper):
        # rotation_distance=0.5 (mock fixture)
        # 10rev → 3600deg → 3600/360 * 0.5mm = 5.0mm
        # 2.0rev/s → 720deg/s → 1.0mm/s
        # 1.0rev/s² → 360deg/s² → 0.5mm/s²
        # rotations_per_ul はこのメソッドの計算には使われないが任意値でインスタンス化する
        dispenser = PasteDispenser(mock_klipper.readonly, rotations_per_ul=0.5)
        gcode = dispenser.rotate_revolutions(rotations=10, rate=2.0, accel=1.0)

        assert isinstance(gcode, GCode)
        lines = gcode.to_list()
        assert lines[0] == f"{PREFIX} SET_POSITION=0.0"
        assert lines[1] == f"{PREFIX} MOVE=5.0 SPEED=1.0 ACCEL=0.5"

    def test_rotate_revolutions_sync_false(self, mock_klipper: Klipper):
        dispenser = PasteDispenser(mock_klipper.readonly, rotations_per_ul=0.5)
        gcode = dispenser.rotate_revolutions(
            rotations=1, rate=1.0, accel=1.0, sync=False
        )

        lines = gcode.to_list()
        assert lines[1].endswith("SYNC=0")
