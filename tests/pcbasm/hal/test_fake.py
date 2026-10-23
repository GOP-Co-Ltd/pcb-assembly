"""`pcbasm.hal.fake` の仕様テスト."""

import numpy as np
import pytest

from pcbasm.config import Audio
from pcbasm.gcode import GCode
from pcbasm.geometry import Point3d
from pcbasm.hal import ManualStepper, XYZStage
from pcbasm.hal.fake import (
    FAKE_PRINTER_CONFIG,
    FakeAudioPlayer,
    FakeCamera,
    FakeKlipper,
    SentGCode,
)
from pcbasm.vision import Image


def _image(value: int) -> Image:
    return Image(np.full((4, 6, 3), value, dtype=np.uint8))


class TestFakeKlipper:
    """送信の記録と、送信から追う位置・押出量."""

    def test_records_each_send_with_effective_timeout(self):
        klipper = FakeKlipper(timeout=60.0)

        klipper.send_gcode("G4 P100\nM400")
        klipper.send_gcode(GCode.wait_for_done(), timeout=5.0)

        assert klipper.sent == (
            SentGCode(GCode("G4 P100\nM400"), 60.0),
            SentGCode(GCode("M400"), 5.0),
        )

    def test_records_emergency_stop_and_firmware_restart(self):
        klipper = FakeKlipper()

        klipper.emergency_stop()
        klipper.firmware_restart()

        assert klipper.sent_lines == ["M112", "FIRMWARE_RESTART"]

    def test_homing_moves_to_endstop_and_marks_axes_homed(self):
        config = {
            **FAKE_PRINTER_CONFIG,
            "stepper_x": {
                "position_min": "0",
                "position_max": "300",
                "position_endstop": "300",
            },
        }
        klipper = FakeKlipper(config)
        klipper.set_status("gcode_move", "gcode_position", [10.0, 20.0, 30.0, 0.0])

        klipper.send_gcode(GCode.homing(x=True))

        assert XYZStage(klipper.readonly).get_position() == Point3d(300.0, 20.0, 30.0)
        assert klipper.get_status("toolhead", "homed_axes") == "x"

        klipper.send_gcode(GCode.homing())

        assert XYZStage(klipper.readonly).get_position() == Point3d(300.0, 0.0, 0.0)
        assert klipper.get_status("toolhead", "homed_axes") == "xyz"

    def test_g1_follows_absolute_and_relative_mode(self):
        klipper = FakeKlipper()
        stage = XYZStage(klipper.readonly)

        klipper.send_gcode(stage.move(x=10.0, y=20.0, z=5.0))
        klipper.send_gcode(["G91", "G1 X1.5 Z-2", "G90"])
        klipper.send_gcode("G1 Y7 F600")

        assert stage.get_position() == Point3d(11.5, 7.0, 3.0)
        assert klipper.g1_moves()[-1] == {"y": 7.0, "f": 600.0}

    def test_manual_stepper_move_accumulates_extrusion(self):
        klipper = FakeKlipper()
        stepper = ManualStepper(klipper.readonly, "paste_dispenser")

        klipper.send_gcode(stepper.reset_position() + stepper.move(2.0))
        klipper.send_gcode(stepper.move(1.5))
        klipper.send_gcode(stepper.reset_position() + stepper.move(0.5))

        assert klipper.extruded == {"paste_dispenser": pytest.approx(2.0)}

    def test_fail_on_raises_like_moonraker_400_for_matching_send(self):
        klipper = FakeKlipper()
        klipper.fail_on(lambda gcode: "M400" in str(gcode))

        klipper.send_gcode("G1 X1")
        with pytest.raises(RuntimeError):
            klipper.send_gcode("G1 X2\nM400")

        assert klipper.sent_lines == ["G1 X1", "G1 X2", "M400"]
        assert XYZStage(klipper.readonly).get_position().x == 1.0

    def test_macros_come_from_printer_config(self):
        klipper = FakeKlipper(
            {
                **FAKE_PRINTER_CONFIG,
                "gcode_macro PRESENT": {"gcode": "G1 Y300"},
            }
        )

        klipper.send_present_or_relax()

        assert klipper.readonly.has_macro("PRESENT")
        assert klipper.sent_lines == ["PRESENT"]


class TestFakeCamera:
    """画像列の再生、例外の注入、fps."""

    def test_returns_frames_in_order_then_repeats_last(self):
        first, second = _image(1), _image(2)
        camera = FakeCamera([first, second])

        assert [camera.capture() for _ in range(3)] == [first, second, second]

    def test_raises_exception_placed_in_frames(self):
        frame = _image(1)
        camera = FakeCamera([OSError("disconnected"), frame])

        with pytest.raises(OSError, match="disconnected"):
            camera.capture()
        assert camera.capture() is frame

    def test_resolution_reports_frame_size_and_fps(self):
        assert FakeCamera([_image(1)]).resolution.size == (6, 4)
        assert FakeCamera([_image(1)]).resolution.fps == 0.0
        assert FakeCamera([_image(1)], fps=15.0).resolution.fps == 15.0

    @pytest.mark.parametrize("frames", [[], [OSError("disconnected")]])
    def test_rejects_frames_without_image(self, frames):
        with pytest.raises(ValueError):
            FakeCamera(frames)


class TestFakeAudioPlayer:
    """再生要求の記録と、再生失敗の注入."""

    def test_records_played_sounds(self):
        player = FakeAudioPlayer()
        config = Audio()

        player.play("success", config).result()
        player.close()

        assert player.played == (("success", config),)
        assert player.close_calls == 1

    def test_playback_error_fails_future(self):
        player = FakeAudioPlayer(playback_error=RuntimeError("no device"))

        with pytest.raises(RuntimeError, match="no device"):
            player.play("failure", Audio()).result()
