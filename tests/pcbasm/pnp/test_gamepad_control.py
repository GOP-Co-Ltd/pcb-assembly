"""`pcbasm.pnp.gamepad_control` の仕様テスト.

Xbox コントローラの入力をピンセットの指令にする。

- LT（0〜1）は Open Close の開き具合（押し込むほど開き、離すと閉じる）。不感帯より小さい変化は送らないが、
  0 と 1 には必ず届ける
- LB / RB は Rotation の - / +。押している間は回り続け、離すと止まる。両方押したら止まる
- run は中止（checkpoint の例外）か切断で終わり、終わるときに回転を止める（トルクは保持する）

コントローラは FakeGamepad、サーボは FakeServoBus（サーボ設定ジョブを済ませた角度制限）を使う。
"""

import pytest

from pcbasm.hal import GamepadError, GamepadState, ServoMode, Tweezer
from pcbasm.hal.fake import FakeGamepad, FakeServoBus, ServoCommand
from pcbasm.pnp.gamepad_control import GamepadControl

ROTATION = 1
OPEN_CLOSE = 2
# 既定の 45°/s・加速度 5000 step/s²: 回り始めは残り 180 step、止めると 27 step だけ残す
CEILING = 180
STOPPING = 27


class _Stop(Exception):
    """テストが run を止めるための中止."""


@pytest.fixture
def bus() -> FakeServoBus:
    # 閉じ切り 1000、開き切り 1500
    return FakeServoBus(
        {ROTATION: ServoMode.STEP, OPEN_CLOSE: ServoMode.POSITION},
        {OPEN_CLOSE: 1000},
        angle_limits={ROTATION: (0, 0), OPEN_CLOSE: (1000, 1500)},
    )


@pytest.fixture
def pad() -> FakeGamepad:
    return FakeGamepad()


@pytest.fixture
def control(bus: FakeServoBus, pad: FakeGamepad) -> GamepadControl:
    tweezer = Tweezer(
        bus,
        rotation_id=ROTATION,
        open_close_id=OPEN_CLOSE,
        closed_position=1000,
        open_position=1500,
        rotation_speed=45.0,
    )
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
