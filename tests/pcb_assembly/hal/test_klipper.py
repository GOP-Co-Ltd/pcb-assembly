from pcb_assembly.hal.klipper import Klipper
from tests.helpers import mark_hardware


class TestKlipper:
    """Klipperクラスのテスト."""

    @mark_hardware
    def test_send_gcode(self):
        klipper = Klipper()

        result = klipper.send_gcode("M115")

        assert "result" in result

    @mark_hardware
    def test_wait_for_move(self):
        klipper = Klipper()

        # M400は例外を発生させずに完了すべき
        klipper.wait_for_move()

    @mark_hardware
    def test_get_status(self):
        klipper = Klipper()

        homed_axes = klipper.get_status("toolhead", "homed_axes")

        assert isinstance(homed_axes, str)

    @mark_hardware
    def test_get_status_gcode_position(self):
        klipper = Klipper()

        position = klipper.get_status("gcode_move", "gcode_position")

        assert isinstance(position, list)
        assert len(position) >= 3  # X, Y, Zはあるはず

    @mark_hardware
    def test_get_config(self):
        klipper = Klipper()

        config = klipper.get_config()

        assert isinstance(config, dict)
