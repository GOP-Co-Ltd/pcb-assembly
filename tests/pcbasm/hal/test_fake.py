"""`pcbasm.hal.fake` の仕様テスト."""

from collections.abc import Callable

import numpy as np
import pytest
from shapely import Point

from pcbasm.gcode import GCode
from pcbasm.geometry import Point2d, Point3d
from pcbasm.hal import (
    DEFAULT_ACCELERATION,
    DEFAULT_SPEED,
    FrameHub,
    GamepadError,
    GamepadState,
    KlipperError,
    ManualStepper,
    ServoError,
    ServoMode,
    XYZStage,
)
from pcbasm.hal.fake import (
    FAKE_PRINTER_CONFIG,
    Checkerboard,
    FakeAudioPlayer,
    FakeCamera,
    FakeGamepad,
    FakeKlipper,
    FakeServoBus,
    SceneCamera,
    SentGCode,
    ServoCommand,
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

    def test_records_emergency_stop_as_m112(self):
        klipper = FakeKlipper()

        klipper.emergency_stop()

        assert klipper.sent_lines == ["M112"]

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

        assert XYZStage(klipper).get_position() == Point3d(300.0, 20.0, 30.0)
        assert klipper.get_status("toolhead", "homed_axes") == "x"

        klipper.send_gcode(GCode.homing())

        assert XYZStage(klipper).get_position() == Point3d(300.0, 0.0, 0.0)
        assert klipper.get_status("toolhead", "homed_axes") == "xyz"

    def test_g1_follows_absolute_and_relative_mode(self):
        klipper = FakeKlipper()
        stage = XYZStage(klipper)

        klipper.send_gcode(stage.move(x=10.0, y=20.0, z=5.0))
        klipper.send_gcode(["G91", "G1 X1.5 Z-2", "G90"])
        klipper.send_gcode("G1 Y7 F600")

        assert stage.get_position() == Point3d(11.5, 7.0, 3.0)
        assert klipper.g1_moves()[-1] == {"y": 7.0, "f": 600.0}

    def test_manual_stepper_move_accumulates_extrusion(self):
        klipper = FakeKlipper()
        stepper = ManualStepper(klipper, "paste_dispenser")

        klipper.send_gcode(stepper.reset_position() + stepper.move(2.0))
        klipper.send_gcode(stepper.move(1.5))
        klipper.send_gcode(stepper.reset_position() + stepper.move(0.5))

        assert klipper.extruded == {"paste_dispenser": pytest.approx(2.0)}

    def test_stepper_moves_record_where_each_move_was_sent(self):
        klipper = FakeKlipper()
        stage = XYZStage(klipper)
        stepper = ManualStepper(klipper, "paste_dispenser")

        klipper.send_gcode(
            stage.move(x=10.0, y=20.0) + stepper.reset_position() + stepper.move(2.0)
        )
        klipper.send_gcode(stepper.move(1.5))

        assert klipper.stepper_moves == (
            StepperMove("paste_dispenser", Point2d(10.0, 20.0), 2.0),
            StepperMove("paste_dispenser", Point2d(10.0, 20.0), -0.5),
        )

    def test_connection_shares_machine_with_its_own_timeout(self):
        """用途ごとに timeout を替えた接続も、1 台の機体の 1 列の送信として記録できる."""
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
        assert XYZStage(klipper).get_position().x == 7.0

    def test_settle_with_waits_after_the_send_is_executed(self):
        klipper = FakeKlipper()
        seen: list[float] = []
        klipper.settle_with(lambda: seen.append(XYZStage(klipper).get_position().x))

        klipper.connection().send_gcode("G1 X3")

        assert seen == [3.0]

    def test_fail_on_raises_like_moonraker_400_for_matching_send(self):
        klipper = FakeKlipper()
        klipper.fail_on(lambda gcode: "M400" in str(gcode))

        klipper.send_gcode("G1 X1")
        with pytest.raises(KlipperError, match="Bad Request"):
            klipper.send_gcode("G1 X2\nM400")

        assert klipper.sent_lines == ["G1 X1", "G1 X2", "M400"]
        assert XYZStage(klipper).get_position().x == 1.0


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
        stepper = ManualStepper(klipper, "paste_dispenser")

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
        stepper = ManualStepper(klipper, "paste_dispenser")

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
        player.play("success", "default", 0.75).result()
        player.close()

        assert player.played == (("success", "default", 0.75),)
        assert player.close_calls == 1

    def test_playback_error_fails_future(self):
        player = FakeAudioPlayer(playback_error=RuntimeError("no device"))

        with pytest.raises(RuntimeError, match="no device"):
            player.play("failure", "default", 0.75).result()


class TestFakeServoBus:
    """指令の記録と、指令から即座に追う位置."""

    @pytest.fixture
    def bus(self) -> FakeServoBus:
        return FakeServoBus({1: ServoMode.STEP, 2: ServoMode.POSITION}, {2: 1000})

    def test_answers_ping_only_for_known_ids(self, bus: FakeServoBus):
        assert bus.ping(1)
        assert bus.ping(2)
        assert not bus.ping(3)

    def test_reports_the_mode_of_each_id(self, bus: FakeServoBus):
        assert bus.mode(1) == ServoMode.STEP
        assert bus.mode(2) == ServoMode.POSITION

    def test_position_defaults_to_the_middle_of_the_raw_range(self, bus: FakeServoBus):
        assert bus.position(1) == 2048
        assert bus.position(2) == 1000

    @pytest.mark.parametrize(
        "operate",
        [
            lambda bus: bus.mode(9),
            lambda bus: bus.position(9),
            lambda bus: bus.moving(9),
            lambda bus: bus.set_torque(9, True),
            lambda bus: bus.goto(9, 100),
            lambda bus: bus.step(9, 100),
        ],
    )
    def test_operations_other_than_ping_on_unknown_id_are_servo_errors(
        self, bus: FakeServoBus, operate: Callable[[FakeServoBus], object]
    ):
        with pytest.raises(ServoError):
            operate(bus)

    def test_speed_is_the_last_one_sent_and_the_default_before_any(
        self, bus: FakeServoBus
    ):
        assert bus.speed(1) == DEFAULT_SPEED

        bus.step(1, 100, speed=1024)
        bus.goto(2, 1100, speed=300)
        bus.step(1, 100)

        assert (bus.speed(1), bus.speed(2)) == (DEFAULT_SPEED, 300)

    @pytest.mark.parametrize("speed", [0, 32768])
    def test_speed_outside_1_to_32767_is_refused(self, bus: FakeServoBus, speed: int):
        with pytest.raises(ValueError):
            bus.step(1, 100, speed=speed)

        assert bus.position(1) == 2048

    def test_remaining_steps_accumulate_and_advance_consumes_them(
        self, bus: FakeServoBus
    ):
        """実機と同じく、動いている途中に送った step は残りに足される."""
        assert bus.remaining_steps(1) == 0

        bus.step(1, 300)
        bus.step(1, 200)
        assert bus.remaining_steps(1) == 500

        bus.advance(1, 120)
        assert bus.remaining_steps(1) == 380
        bus.step(1, -1000)
        bus.advance(1, 2000)  # 残りより多く進めても、goal で止まる
        assert bus.remaining_steps(1) == 0

    def test_step_beyond_15_bits_is_refused(self, bus: FakeServoBus):
        with pytest.raises(ValueError):
            bus.step(1, 32768)

        assert bus.remaining_steps(1) == 0

    def test_advance_refuses_negative_steps(self, bus: FakeServoBus):
        bus.step(1, 100)

        with pytest.raises(ValueError):
            bus.advance(1, -10)

        assert bus.remaining_steps(1) == 100

    def test_acceleration_is_the_last_one_sent(self, bus: FakeServoBus):
        assert bus.acceleration(1) == DEFAULT_ACCELERATION

        bus.step(1, 100, acceleration=50)
        bus.goto(2, 1100, acceleration=100)

        assert (bus.acceleration(1), bus.acceleration(2)) == (50, 100)

    @pytest.mark.parametrize("acceleration", [0, 255])
    def test_acceleration_outside_1_to_254_is_refused(
        self, bus: FakeServoBus, acceleration: int
    ):
        with pytest.raises(ValueError):
            bus.step(1, 100, acceleration=acceleration)

        assert bus.remaining_steps(1) == 0

    def test_torque_enabled_follows_set_torque_and_release_all(self, bus: FakeServoBus):
        assert not bus.torque_enabled(1)

        bus.set_torque(1, True)
        assert bus.torque_enabled(1)
        bus.release_all()
        assert not bus.torque_enabled(1)

    def test_goto_is_refused_in_step_mode(self, bus: FakeServoBus):
        with pytest.raises(ServoError):
            bus.goto(1, 100)

        assert bus.position(1) == 2048

    def test_step_is_refused_in_position_mode(self, bus: FakeServoBus):
        with pytest.raises(ServoError):
            bus.step(2, 100)

        assert bus.position(2) == 1000

    @pytest.mark.parametrize("position", [-1, 4096])
    def test_goto_outside_the_raw_range_is_refused_without_recording(
        self, bus: FakeServoBus, position: int
    ):
        with pytest.raises(ValueError):
            bus.goto(2, position)

        assert bus.commands == []
        assert bus.position(2) == 1000

    def test_goto_moves_to_the_absolute_position_at_once(self, bus: FakeServoBus):
        bus.goto(2, 3000)

        assert bus.position(2) == 3000

    def test_step_moves_relatively_and_wraps_at_one_revolution(self):
        bus = FakeServoBus({1: ServoMode.STEP}, {1: 4000})

        bus.step(1, 200)
        assert bus.position(1) == 104

        bus.step(1, -300)
        assert bus.position(1) == 3900

        bus.step(1, 4096 * 3)
        assert bus.position(1) == 3900

    def test_is_not_moving_without_a_command(self, bus: FakeServoBus):
        assert not bus.moving(1)
        assert not bus.moving(2)

    @pytest.mark.parametrize(
        ("servo_id", "move"),
        [(2, lambda bus: bus.goto(2, 3000)), (1, lambda bus: bus.step(1, 100))],
    )
    def test_reports_moving_once_after_a_command_then_stops(
        self,
        bus: FakeServoBus,
        servo_id: int,
        move: Callable[[FakeServoBus], None],
    ):
        move(bus)

        assert [bus.moving(servo_id) for _ in range(3)] == [True, False, False]

    def test_start_delay_holds_moving_off_for_that_many_reads(self):
        bus = FakeServoBus({2: ServoMode.POSITION}, start_delay=2)
        bus.goto(2, 3000)

        assert [bus.moving(2) for _ in range(5)] == [False, False, True, False, False]

    def test_jammed_servo_keeps_moving(self, bus: FakeServoBus):
        bus.jam(2)

        assert bus.moving(2)
        assert not bus.moving(1)

    def test_jam_of_an_unanswered_id_is_a_servo_error(self, bus: FakeServoBus):
        with pytest.raises(ServoError):
            bus.jam(9)

    def test_set_mode_changes_the_mode_and_is_recorded(self, bus: FakeServoBus):
        bus.set_mode(2, ServoMode.STEP)

        assert bus.mode(2) == ServoMode.STEP
        assert bus.commands == [ServoCommand(2, "mode", 3)]

    def test_release_all_turns_every_torque_off(self, bus: FakeServoBus):
        bus.set_torque(1, True)
        bus.set_torque(2, True)

        bus.release_all()

        assert bus.commands[-1] == ServoCommand(254, "torque", 0)
        bus.turn_by_hand(1, 100)  # トルクが切れているので手で回せる
        bus.turn_by_hand(2, 200)
        assert (bus.position(1), bus.position(2)) == (100, 200)

    def test_goto_stops_at_the_angle_limits(self, bus: FakeServoBus):
        bus.set_angle_limits(2, 900, 1200)

        bus.goto(2, 1500)
        assert bus.position(2) == 1200
        bus.goto(2, 100)
        assert bus.position(2) == 900
        assert bus.angle_limits(2) == (900, 1200)
        assert bus.commands[:2] == [
            ServoCommand(2, "min_angle", 900),
            ServoCommand(2, "max_angle", 1200),
        ]

    def test_initial_angle_limits_can_be_given(self):
        bus = FakeServoBus(
            {1: ServoMode.STEP, 2: ServoMode.POSITION}, angle_limits={2: (900, 1200)}
        )

        assert (bus.angle_limits(1), bus.angle_limits(2)) == ((0, 4095), (900, 1200))
        assert bus.commands == []

    def test_clear_angle_limits_writes_zero_and_zero(self, bus: FakeServoBus):
        bus.clear_angle_limits(1)

        assert bus.angle_limits(1) == (0, 0)
        assert bus.commands == [
            ServoCommand(1, "min_angle", 0),
            ServoCommand(1, "max_angle", 0),
        ]

    def test_goto_without_angle_limits_is_not_clamped(self, bus: FakeServoBus):
        bus.clear_angle_limits(2)

        bus.goto(2, 3000)

        assert bus.position(2) == 3000

    def test_goal_is_the_last_goto_and_the_position_before_any(self, bus: FakeServoBus):
        assert bus.goal(2) == 1000

        bus.goto(2, 1100)
        bus.set_torque(2, False)
        bus.turn_by_hand(2, 1500)

        assert (bus.goal(2), bus.position(2)) == (1100, 1500)

    def test_goal_is_refused_in_step_mode(self, bus: FakeServoBus):
        with pytest.raises(ServoError):
            bus.goal(1)

    def test_unusable_angle_limits_are_refused(self, bus: FakeServoBus):
        with pytest.raises(ValueError):
            bus.set_angle_limits(2, 1200, 900)

        assert bus.angle_limits(2) == (0, 4095)

    def test_commands_is_a_snapshot(self, bus: FakeServoBus):
        commands = bus.commands
        bus.set_torque(1, True)

        assert commands == []

    def test_records_goto_step_and_torque_in_order(self, bus: FakeServoBus):
        bus.set_torque(1, True)
        bus.step(1, -10)
        bus.goto(2, 1100)
        bus.set_torque(2, False)

        assert bus.commands == [
            ServoCommand(1, "torque", 1),
            ServoCommand(1, "step", -10),
            ServoCommand(2, "goto", 1100),
            ServoCommand(2, "torque", 0),
        ]

    def test_turn_by_hand_moves_a_released_servo_without_a_command(
        self, bus: FakeServoBus
    ):
        bus.set_torque(2, False)

        bus.turn_by_hand(2, 1500)

        assert bus.position(2) == 1500
        assert bus.commands == [ServoCommand(2, "torque", 0)]

    def test_turn_by_hand_is_refused_while_the_torque_is_on(self, bus: FakeServoBus):
        bus.set_torque(2, True)

        with pytest.raises(ServoError):
            bus.turn_by_hand(2, 1500)

        assert bus.position(2) == 1000

    def test_close_marks_the_bus_closed(self, bus: FakeServoBus):
        assert not bus.closed

        bus.close()

        assert bus.closed


class TestFakeGamepad:
    """テストが設定した入力を返し、切断も模擬する."""

    def test_is_at_rest_until_set(self):
        assert FakeGamepad().state() == GamepadState()

    def test_returns_the_state_set_by_the_test(self):
        gamepad = FakeGamepad()
        pressed = GamepadState(left_trigger=0.3, left_bumper=True, right_bumper=False)

        gamepad.set_state(pressed)

        assert gamepad.state() == pressed

    def test_disconnect_makes_state_raise(self):
        gamepad = FakeGamepad(GamepadState(left_trigger=1.0))

        gamepad.disconnect()

        with pytest.raises(GamepadError, match="切断"):
            gamepad.state()

    def test_close_marks_the_gamepad_closed(self):
        gamepad = FakeGamepad()
        assert not gamepad.closed

        gamepad.close()

        assert gamepad.closed
