"""`pcbasm.pnp.gamepad_control` の仕様テスト.

Xbox コントローラの入力をピンセットの指令にする。

- LT（0〜1）は Open Close の開き具合（押し込むほど開き、離すと閉じる）。不感帯より小さい変化は送らないが、
  0 と 1 には必ず届ける
- LB / RB は Rotation の - / +。押している間は回り続け、離すと止まる。両方押したら止まる
- run は中止（checkpoint の例外）か切断で終わり、終わるときに回転を止める（トルクは保持する）
- XYZ のステージがあれば、左スティックで XY、右スティックの縦で Z の速度を毎周期送る
  （上 = Y−、右 = X+、右スティックの上 = Z+。倒し切ると各軸の最高速度）。原点が未の軸は 0 にする
- 画面の仮想スティックは、期限（VIRTUAL_TIMEOUT）の間だけゲームパッドより優先し、デッドゾーンを掛けない
- ステージがあれば、コントローラが無くても外れても止まらず、仮想スティックだけで続ける。
  終わるときは回転とステージの両方を止めようとする

コントローラは FakeGamepad、サーボは FakeServoBus（サーボ設定ジョブを済ませた角度制限）、
ステージは FakeMotionStage（手で進める時計）を使う。
"""

import logging

import pytest

from pcbasm.config.pnp import GamepadConfig
from pcbasm.hal import (
    GamepadError,
    GamepadState,
    Limits,
    ScalarLimits,
    ServoMode,
    Tweezer,
    Velocity,
)
from pcbasm.hal.fake import FakeGamepad, FakeMotionStage, FakeServoBus, ServoCommand
from pcbasm.pnp.gamepad_control import VIRTUAL_TIMEOUT, GamepadControl
from pcbasm.pnp.stick import StickInput

ROTATION = 1
OPEN_CLOSE = 2
# 既定の 45°/s・加速度 5000 step/s²: 回り始めは残り 180 step、止めると 27 step だけ残す
CEILING = 180
STOPPING = 27


class _Stop(Exception):
    """テストが run を止めるための中止."""


@pytest.fixture
def bus() -> FakeServoBus:
    # 閉じ切り 1000、開き切り 1500。Open Close はトルクが入っている（パネルなどで一度動かした後）
    bus = FakeServoBus(
        {ROTATION: ServoMode.STEP, OPEN_CLOSE: ServoMode.POSITION},
        {OPEN_CLOSE: 1000},
        angle_limits={ROTATION: (0, 0), OPEN_CLOSE: (1000, 1500)},
    )
    bus.set_torque(OPEN_CLOSE, True)
    return bus


@pytest.fixture
def pad() -> FakeGamepad:
    return FakeGamepad()


@pytest.fixture
def tweezer(bus: FakeServoBus) -> Tweezer:
    return Tweezer(
        bus,
        rotation_id=ROTATION,
        open_close_id=OPEN_CLOSE,
        closed_position=1000,
        open_position=1500,
        rotation_speed=45.0,
    )


@pytest.fixture
def control(tweezer: Tweezer, pad: FakeGamepad) -> GamepadControl:
    return GamepadControl(pad, tweezer)


def _gotos(bus: FakeServoBus) -> list[int]:
    return [c.value for c in bus.commands if c.kind == "goto"]


class TestOpenClose:
    def test_first_step_sends_the_trigger_even_when_released(
        self, control: GamepadControl, bus: FakeServoBus
    ):
        control.step()

        assert _gotos(bus) == [1000]

    @pytest.mark.parametrize(("trigger", "target"), [(0.5, 1250), (1.0, 1500)])
    def test_trigger_sets_the_opening(
        self,
        control: GamepadControl,
        bus: FakeServoBus,
        pad: FakeGamepad,
        trigger: float,
        target: int,
    ):
        pad.set_state(GamepadState(left_trigger=trigger))

        control.step()

        assert _gotos(bus) == [target]

    def test_unchanged_trigger_sends_nothing(
        self, control: GamepadControl, bus: FakeServoBus, pad: FakeGamepad
    ):
        pad.set_state(GamepadState(left_trigger=0.5))
        control.step()

        control.step()
        pad.set_state(GamepadState(left_trigger=0.505))  # 不感帯（0.01）より小さい
        control.step()

        assert _gotos(bus) == [1250]

    def test_release_within_the_deadband_still_closes_fully(
        self, control: GamepadControl, bus: FakeServoBus, pad: FakeGamepad
    ):
        pad.set_state(GamepadState(left_trigger=0.005))
        control.step()

        pad.set_state(GamepadState(left_trigger=0.0))
        control.step()

        assert _gotos(bus) == [1002, 1000]


class TestRotation:
    @pytest.mark.parametrize(
        ("state", "direction"),
        [
            (GamepadState(right_bumper=True), 1),
            (GamepadState(left_bumper=True), -1),
        ],
    )
    def test_bumper_keeps_rotating_its_way(
        self,
        control: GamepadControl,
        bus: FakeServoBus,
        pad: FakeGamepad,
        state: GamepadState,
        direction: int,
    ):
        pad.set_state(state)

        control.step()

        assert bus.remaining_steps(ROTATION) == direction * CEILING

    def test_holding_the_bumper_tops_up_as_the_servo_turns(
        self, control: GamepadControl, bus: FakeServoBus, pad: FakeGamepad
    ):
        pad.set_state(GamepadState(right_bumper=True))
        control.step()

        for _ in range(5):
            bus.advance(ROTATION, 60)
            control.step()

        assert bus.remaining_steps(ROTATION) >= CEILING - 60

    def test_releasing_the_bumper_stops(
        self, control: GamepadControl, bus: FakeServoBus, pad: FakeGamepad
    ):
        pad.set_state(GamepadState(right_bumper=True))
        control.step()

        pad.set_state(GamepadState())
        control.step()

        assert bus.remaining_steps(ROTATION) == STOPPING

    def test_both_bumpers_stop(
        self, control: GamepadControl, bus: FakeServoBus, pad: FakeGamepad
    ):
        pad.set_state(GamepadState(right_bumper=True))
        control.step()

        pad.set_state(GamepadState(left_bumper=True, right_bumper=True))
        control.step()

        assert bus.remaining_steps(ROTATION) == STOPPING

    def test_released_bumpers_send_no_rotation(
        self, control: GamepadControl, bus: FakeServoBus
    ):
        control.step()
        control.step()

        assert [c for c in bus.commands if c.servo_id == ROTATION] == []


class TestRun:
    def test_runs_until_the_checkpoint_raises_and_stops_rotating(
        self, control: GamepadControl, bus: FakeServoBus, pad: FakeGamepad
    ):
        pad.set_state(GamepadState(right_bumper=True, left_trigger=1.0))
        calls = 0

        def checkpoint() -> None:
            nonlocal calls
            calls += 1
            if calls > 3:
                raise _Stop

        with pytest.raises(_Stop):
            control.run(checkpoint, period=0.001)

        assert bus.remaining_steps(ROTATION) == STOPPING
        # 中止してもトルクは切らない（つまんだ部品を落とさない）
        assert ServoCommand(254, "torque", 0) not in bus.commands
        assert _gotos(bus) == [1500]

    def test_failing_to_stop_keeps_the_original_exception(
        self, control: GamepadControl, bus: FakeServoBus, pad: FakeGamepad
    ):
        """止めるのに失敗しても、中止（ジョブなら ABORTED）を失わない."""
        pad.set_state(GamepadState(right_bumper=True))

        def checkpoint() -> None:
            if bus.commands:
                bus.set_mode(ROTATION, ServoMode.POSITION)  # 以後、残りを読めない
                raise _Stop

        with pytest.raises(_Stop):
            control.run(checkpoint, period=0.001)

    def test_disconnect_ends_the_run_and_stops_rotating(
        self, control: GamepadControl, bus: FakeServoBus, pad: FakeGamepad
    ):
        pad.set_state(GamepadState(left_bumper=True))
        calls = 0

        def checkpoint() -> None:
            nonlocal calls
            calls += 1
            if calls > 2:
                pad.disconnect()

        with pytest.raises(GamepadError):
            control.run(checkpoint, period=0.001)

        assert bus.remaining_steps(ROTATION) == -STOPPING


LIMITS = Limits(
    x=ScalarLimits(0.0, 100.0),
    y=ScalarLimits(0.0, 80.0),
    z=ScalarLimits(-40.0, 0.0),
    v=ScalarLimits(0.0, 50.0),
)
MAX_VELOCITY = Velocity(x=50.0, y=40.0, z=10.0)
NO_DEADZONE = GamepadConfig(deadzone_xy=0.0, deadzone_z=0.0)


class _Clock:
    """テストが手で進める時計."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def stage(clock: _Clock) -> FakeMotionStage:
    return FakeMotionStage(LIMITS, MAX_VELOCITY, homed=True, clock=clock)


def _sent(stage: FakeMotionStage) -> list[Velocity]:
    return [velocity for _, velocity in stage.velocities]


class TestStickVelocity:
    """ゲームパッドのスティックを XYZ の速度にする."""

    @pytest.mark.parametrize(
        ("state", "velocity"),
        [
            (GamepadState(left_x=1.0), Velocity(x=50.0)),
            (GamepadState(left_y=1.0), Velocity(y=-40.0)),  # 上は Y−
            (GamepadState(left_x=-0.5), Velocity(x=-25.0)),
            (GamepadState(right_y=1.0), Velocity(z=10.0)),  # 上は Z+
            (GamepadState(right_y=-0.5), Velocity(z=-5.0)),
        ],
    )
    def test_stick_maps_to_the_machine_axes(
        self,
        pad: FakeGamepad,
        stage: FakeMotionStage,
        clock: _Clock,
        state: GamepadState,
        velocity: Velocity,
    ):
        pad.set_state(state)
        control = GamepadControl(
            pad, None, motion=stage, settings=NO_DEADZONE, clock=clock
        )

        control.step()

        assert _sent(stage) == [velocity]

    def test_sends_every_step_even_when_unchanged(
        self, pad: FakeGamepad, stage: FakeMotionStage, clock: _Clock
    ):
        """期限（ttl）で止まらないよう、同じ値でも毎周期送り直す."""
        pad.set_state(GamepadState(left_x=1.0))
        control = GamepadControl(
            pad, None, motion=stage, settings=NO_DEADZONE, clock=clock
        )

        for _ in range(3):
            control.step()
            clock.now += 0.02

        assert _sent(stage) == [Velocity(x=50.0)] * 3

    def test_releasing_the_stick_sends_zero(
        self, pad: FakeGamepad, stage: FakeMotionStage, clock: _Clock
    ):
        pad.set_state(GamepadState(left_x=1.0))
        control = GamepadControl(pad, None, motion=stage, clock=clock)
        control.step()

        pad.set_state(GamepadState())
        control.step()

        assert _sent(stage) == [Velocity(x=50.0), Velocity()]

    def test_deadzone_from_the_settings_drops_the_rest_offset(
        self, pad: FakeGamepad, stage: FakeMotionStage, clock: _Clock
    ):
        """静止しても残るずれ（0.02 程度）は動かさない。外は 0〜1 へ伸ばし直す."""
        settings = GamepadConfig(deadzone_xy=0.2, deadzone_z=0.5)
        pad.set_state(GamepadState(left_x=0.6, left_y=0.02, right_y=0.75))
        control = GamepadControl(
            pad, None, motion=stage, settings=settings, clock=clock
        )
        control.step()
        pad.set_state(GamepadState(left_x=0.02, left_y=-0.02, right_y=0.02))
        control.step()

        first, second = _sent(stage)
        assert first.x == pytest.approx(50.0 * (0.6 - 0.2) / 0.8, abs=0.1)
        assert first.z == pytest.approx(5.0)
        assert second == Velocity()

    def test_trigger_and_stick_work_together(
        self,
        tweezer: Tweezer,
        bus: FakeServoBus,
        pad: FakeGamepad,
        stage: FakeMotionStage,
        clock: _Clock,
    ):
        pad.set_state(GamepadState(left_trigger=1.0, left_x=1.0, right_bumper=True))
        control = GamepadControl(
            pad, tweezer, motion=stage, settings=NO_DEADZONE, clock=clock
        )

        control.step()

        assert _gotos(bus) == [1500]
        assert bus.remaining_steps(ROTATION) == CEILING
        assert _sent(stage) == [Velocity(x=50.0)]


class TestUnhomedAxes:
    def test_unhomed_axes_are_dropped_to_zero(self, pad: FakeGamepad, clock: _Clock):
        stage = FakeMotionStage(LIMITS, MAX_VELOCITY, clock=clock)
        stage.home(["z"])
        pad.set_state(GamepadState(left_x=1.0, right_y=1.0))
        control = GamepadControl(
            pad, None, motion=stage, settings=NO_DEADZONE, clock=clock
        )

        control.step()

        assert _sent(stage) == [Velocity(z=10.0)]

    def test_logs_the_unhomed_axes_only_once(
        self, pad: FakeGamepad, clock: _Clock, caplog: pytest.LogCaptureFixture
    ):
        stage = FakeMotionStage(LIMITS, MAX_VELOCITY, clock=clock)
        pad.set_state(GamepadState(left_x=1.0))
        control = GamepadControl(
            pad, None, motion=stage, settings=NO_DEADZONE, clock=clock
        )

        with caplog.at_level(logging.WARNING, logger="pcbasm.pnp.gamepad_control"):
            for _ in range(3):
                control.step()

        assert stage.velocities == []
        assert len([r for r in caplog.records if "原点" in r.getMessage()]) == 1


class TestVirtualStick:
    """画面の仮想スティック（ジョブが WS の命令を set_virtual に渡す）."""

    def test_virtual_stick_moves_without_deadzone(
        self, pad: FakeGamepad, stage: FakeMotionStage, clock: _Clock
    ):
        control = GamepadControl(pad, None, motion=stage, clock=clock)

        control.set_virtual(StickInput(x=0.1, y=0.5, z=-1.0))
        control.step()

        assert _sent(stage) == [Velocity(x=5.0, y=-20.0, z=-10.0)]

    def test_virtual_stick_overrides_the_gamepad(
        self, pad: FakeGamepad, stage: FakeMotionStage, clock: _Clock
    ):
        pad.set_state(GamepadState(left_x=1.0))
        control = GamepadControl(
            pad, None, motion=stage, settings=NO_DEADZONE, clock=clock
        )

        control.set_virtual(StickInput(y=1.0))
        control.step()

        assert _sent(stage) == [Velocity(y=-40.0)]

    def test_neutral_virtual_stick_leaves_the_gamepad_in_charge(
        self, pad: FakeGamepad, stage: FakeMotionStage, clock: _Clock
    ):
        pad.set_state(GamepadState(left_x=1.0))
        control = GamepadControl(
            pad, None, motion=stage, settings=NO_DEADZONE, clock=clock
        )

        control.set_virtual(StickInput())
        control.step()

        assert _sent(stage) == [Velocity(x=50.0)]

    def test_virtual_stick_expires_to_neutral(
        self, pad: FakeGamepad, stage: FakeMotionStage, clock: _Clock
    ):
        """送信が途切れたら（タブを閉じたなど）、期限を過ぎて 0 になる."""
        control = GamepadControl(pad, None, motion=stage, clock=clock)
        control.set_virtual(StickInput(x=1.0))

        clock.now += VIRTUAL_TIMEOUT - 0.01
        control.step()
        clock.now += 0.02
        control.step()

        assert _sent(stage) == [Velocity(x=50.0), Velocity()]


class TestWithoutGamepad:
    """ステージがあれば、物理コントローラが無くても外れても続ける."""

    def test_runs_on_the_virtual_stick_without_a_gamepad(
        self, stage: FakeMotionStage, clock: _Clock
    ):
        control = GamepadControl(None, None, motion=stage, clock=clock)

        control.step()
        control.set_virtual(StickInput(x=-1.0))
        control.step()

        assert _sent(stage) == [Velocity(x=-50.0)]

    def test_disconnect_stops_the_stick_and_keeps_going(
        self,
        tweezer: Tweezer,
        bus: FakeServoBus,
        pad: FakeGamepad,
        stage: FakeMotionStage,
        clock: _Clock,
    ):
        pad.set_state(GamepadState(left_x=1.0, right_bumper=True))
        control = GamepadControl(
            pad, tweezer, motion=stage, settings=NO_DEADZONE, clock=clock
        )
        control.step()

        pad.disconnect()
        control.step()
        control.set_virtual(StickInput(z=1.0))
        control.step()

        assert _sent(stage) == [Velocity(x=50.0), Velocity(), Velocity(z=10.0)]
        assert bus.remaining_steps(ROTATION) == STOPPING

    def test_disconnect_logs_once(
        self,
        pad: FakeGamepad,
        stage: FakeMotionStage,
        clock: _Clock,
        caplog: pytest.LogCaptureFixture,
    ):
        control = GamepadControl(pad, None, motion=stage, clock=clock)
        pad.disconnect()

        with caplog.at_level(logging.WARNING, logger="pcbasm.pnp.gamepad_control"):
            for _ in range(3):
                control.step()

        assert len(caplog.records) == 1


class TestRunWithMotion:
    def test_stops_both_rotation_and_stage_at_the_end(
        self,
        tweezer: Tweezer,
        bus: FakeServoBus,
        pad: FakeGamepad,
        stage: FakeMotionStage,
        clock: _Clock,
    ):
        pad.set_state(GamepadState(left_x=1.0, right_bumper=True))
        control = GamepadControl(pad, tweezer, motion=stage, clock=clock)
        calls = 0

        def checkpoint() -> None:
            nonlocal calls
            calls += 1
            if calls > 2:
                raise _Stop

        with pytest.raises(_Stop):
            control.run(checkpoint, period=0.001)

        assert stage.commands == ["stop"]
        assert bus.remaining_steps(ROTATION) == STOPPING

    def test_failing_stage_stop_still_stops_rotation(
        self,
        tweezer: Tweezer,
        bus: FakeServoBus,
        pad: FakeGamepad,
        stage: FakeMotionStage,
        clock: _Clock,
    ):
        pad.set_state(GamepadState(right_bumper=True))
        control = GamepadControl(pad, tweezer, motion=stage, clock=clock)

        calls = 0

        def checkpoint() -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                stage.disconnect()  # 以後、ステージは MotionError
                raise _Stop

        with pytest.raises(_Stop):
            control.run(checkpoint, period=0.001)

        assert bus.remaining_steps(ROTATION) == STOPPING

    def test_failing_rotation_stop_still_stops_the_stage(
        self,
        tweezer: Tweezer,
        bus: FakeServoBus,
        pad: FakeGamepad,
        stage: FakeMotionStage,
        clock: _Clock,
    ):
        pad.set_state(GamepadState(right_bumper=True))
        control = GamepadControl(pad, tweezer, motion=stage, clock=clock)

        def checkpoint() -> None:
            if bus.commands:
                bus.set_mode(ROTATION, ServoMode.POSITION)  # 以後、残りを読めない
                raise _Stop

        with pytest.raises(_Stop):
            control.run(checkpoint, period=0.001)

        assert stage.commands == ["stop"]

    def test_run_without_tweezer_stops_the_stage(
        self, stage: FakeMotionStage, clock: _Clock
    ):
        control = GamepadControl(None, None, motion=stage, clock=clock)

        def checkpoint() -> None:
            raise _Stop

        with pytest.raises(_Stop):
            control.run(checkpoint, period=0.001)

        assert stage.commands == ["stop"]
