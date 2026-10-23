"""`pcbasm.hal.fake` の仕様テスト."""

from collections.abc import Callable

import numpy as np
import pytest
from shapely import Point

from pcbasm.config import Audio
from pcbasm.gcode import GCode
from pcbasm.geometry import Point2d, Point3d
from pcbasm.hal import FrameHub, ManualStepper, XYZStage
from pcbasm.hal.fake import (
    FAKE_PRINTER_CONFIG,
    Checkerboard,
    FakeAudioPlayer,
    FakeCamera,
    FakeKlipper,
    SceneCamera,
    SentGCode,
    StepperMove,
)
from pcbasm.vision import CheckerboardCalibrator, Image, ImageArray


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

    def test_stepper_moves_record_where_each_move_was_sent(self):
        klipper = FakeKlipper()
        stage = XYZStage(klipper.readonly)
        stepper = ManualStepper(klipper.readonly, "paste_dispenser")

        klipper.send_gcode(
            stage.move(x=10.0, y=20.0) + stepper.reset_position() + stepper.move(2.0)
        )
        klipper.send_gcode(stepper.move(1.5))

        assert klipper.stepper_moves == (
            StepperMove("paste_dispenser", Point2d(10.0, 20.0), 2.0),
            StepperMove("paste_dispenser", Point2d(10.0, 20.0), -0.5),
        )

    def test_connection_shares_machine_with_its_own_timeout(self):
        """``open_klipper`` を開き直すジョブも、1 台の機体の 1 列の送信として記録できる."""
        klipper = FakeKlipper(timeout=60.0)
        connection = klipper.connection(timeout=30.0)

        connection.send_gcode("G1 X5")
        klipper.send_gcode("G91\nG1 X1")
        connection.send_gcode("G1 X1")

        assert klipper.sent == (
            SentGCode(GCode("G1 X5"), 30.0),
            SentGCode(GCode("G91\nG1 X1"), 60.0),
            SentGCode(GCode("G1 X1"), 30.0),
        )
        assert XYZStage(klipper.readonly).get_position().x == 7.0

    def test_settle_with_waits_after_the_send_is_executed(self):
        klipper = FakeKlipper()
        seen: list[float] = []
        klipper.settle_with(
            lambda: seen.append(XYZStage(klipper.readonly).get_position().x)
        )

        klipper.connection().send_gcode("G1 X3")

        assert seen == [3.0]

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


def _centroid(image: Image, select: Callable[[ImageArray], ImageArray]) -> Point2d:
    """選んだ画素の重心（画素座標）."""
    ys, xs = np.nonzero(select(image.numpy()[:, :, 0]))
    return Point2d(float(xs.mean()), float(ys.mean()))


def _bright(gray: ImageArray) -> ImageArray:
    return gray > 155


def _dark(gray: ImageArray) -> ImageArray:
    return gray < 60


class TestSceneCamera:
    """ステージ位置から見える図形と塗布痕の描画、FrameHub での静定待ち."""

    @pytest.fixture
    def klipper(self) -> FakeKlipper:
        klipper = FakeKlipper()
        klipper.set_status("gcode_move", "gcode_position", [50.0, 50.0, 0.0, 0.0])
        return klipper

    def _camera(
        self,
        klipper: FakeKlipper,
        *,
        toolhead: Point2d = Point2d(0.0, 0.0),
        rotations_per_ul: float = 1.0,
        size: tuple[int, int] = (200, 100),
        **kwargs,
    ) -> SceneCamera:
        return SceneCamera(
            klipper,
            pixel_per_mm=20.0,
            toolhead=toolhead,
            rotations_per_ul=rotations_per_ul,
            size=size,
            fps=1000.0,
            **kwargs,
        )

    @pytest.mark.parametrize(
        ("rotation", "expected"),
        [(0.0, Point2d(60.0, 50.0)), (90.0, Point2d(100.0, 10.0))],
    )
    def test_maps_machine_point_from_stage_position(
        self, klipper: FakeKlipper, rotation: float, expected: Point2d
    ):
        """点 p は画像中心から ppm·Rot(rotation)(s − p) に写る（+X 側の点は回転 0 で左）."""
        camera = self._camera(
            klipper, rotation=rotation, shapes=[Point(52.0, 50.0).buffer(0.5, 32)]
        )

        center = _centroid(camera.capture(), _bright)

        assert (center - expected).norm < 0.1

    def test_follows_stage(self, klipper: FakeKlipper):
        camera = self._camera(klipper, shapes=[Point(52.0, 50.0).buffer(0.5, 32)])

        klipper.send_gcode("G1 X51")

        assert (_centroid(camera.capture(), _bright) - Point2d(80.0, 50.0)).norm < 0.1

    def test_draws_net_dispense_as_hemisphere_seen_from_camera(
        self, klipper: FakeKlipper
    ):
        """押し出して戻した差し引きを、ノズルから toolhead だけ戻した点に半球の径で描く."""
        camera = self._camera(
            klipper, toolhead=Point2d(1.0, 0.0), rotations_per_ul=10.0
        )
        stepper = ManualStepper(klipper.readonly, "paste_dispenser")

        klipper.send_gcode(stepper.reset_position() + stepper.move(2.0))
        klipper.send_gcode(stepper.move(1.0))
        image = camera.capture()

        # 0.1 μL の半球の半径は約 0.363 mm（7.3 px）
        radius_px = np.sqrt(np.count_nonzero(_dark(image.numpy()[:, :, 0])) / np.pi)
        assert (_centroid(image, _dark) - Point2d(120.0, 50.0)).norm < 0.1
        assert radius_px == pytest.approx(7.26, abs=0.5)

    def test_retract_before_dispensing_does_not_hide_the_dispense(
        self, klipper: FakeKlipper
    ):
        """押し出す前の引き戻しは基板から何も吸い戻さないので、後で同じ XY に押し出した分はそのまま描く."""
        camera = self._camera(klipper, rotations_per_ul=10.0)
        stepper = ManualStepper(klipper.readonly, "paste_dispenser")

        klipper.send_gcode(stepper.reset_position() + stepper.move(-1.0))
        klipper.send_gcode(stepper.reset_position() + stepper.move(1.0))
        image = camera.capture()

        radius_px = np.sqrt(np.count_nonzero(_dark(image.numpy()[:, :, 0])) / np.pi)
        assert radius_px == pytest.approx(7.26, abs=0.5)

    def test_checkerboard_calibrates_to_pixel_per_mm(self, klipper: FakeKlipper):
        camera = self._camera(
            klipper,
            size=(400, 300),
            checkerboard=Checkerboard(center=Point2d(50.0, 50.0), square=1.5),
        )

        detection = CheckerboardCalibrator(1.5, (300, 300)).calibrate(camera.capture())

        assert detection is not None
        assert detection[0].pixel_per_mm == pytest.approx(20.0, rel=1e-2)

    def test_frame_hub_delivers_new_position_after_each_send(
        self, klipper: FakeKlipper
    ):
        """FrameHub が撮っている間は、送信が返った後に撮るフレームが必ず新しい位置で描かれている."""
        camera = self._camera(klipper, shapes=[Point(50.0, 50.0).buffer(0.5, 32)])
        hub = FrameHub(camera)
        hub.start()
        try:
            source = hub.subscribe()
            watcher = hub.subscribe()
            source.capture()
            for step in range(1, 11):
                # 送信の前に描いたフレームを FrameHub に配らせておく
                watcher.capture()
                watcher.capture()
                klipper.send_gcode(f"G1 X{50.0 + 0.1 * step}")

                center = _centroid(source.capture(), _bright)

                assert (center - Point2d(100.0 + 2.0 * step, 50.0)).norm < 0.1
        finally:
            hub.stop()


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
