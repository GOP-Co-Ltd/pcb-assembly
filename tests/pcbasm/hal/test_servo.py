"""`pcbasm.hal.servo` の仕様テスト.

ピンセットのサーボ（Feetech STS3032 ×2）。回転はステップモード、開閉は位置モードで動かす。
バスは `FakeServoBus` を使う。`FeetechBus` は実機が要るので、開けないポートの扱いだけを確かめる。
"""

from pathlib import Path
from typing import override

import pytest

from pcbasm.hal import (
    ServoError,
    ServoMode,
    Tweezer,
    degrees_to_steps,
    scan,
    servo as servo_module,
    wiggle,
)
from pcbasm.hal.fake import FakeServoBus, ServoCommand

ROTATION = 1
OPEN_CLOSE = 2


def _bus(open_close_position: int = 1000) -> FakeServoBus:
    """回転が ID 1（ステップモード）、開閉が ID 2（位置モード）のバス."""
    return FakeServoBus(
        {ROTATION: ServoMode.STEP, OPEN_CLOSE: ServoMode.POSITION},
        {OPEN_CLOSE: open_close_position},
    )


def _tweezer(
    bus: FakeServoBus, *, closed_position: int = 1000, open_position: int = 1500
) -> Tweezer:
    return Tweezer(
        bus,
        rotation_id=ROTATION,
        open_close_id=OPEN_CLOSE,
        closed_position=closed_position,
        open_position=open_position,
    )


def _moves(bus: FakeServoBus) -> list[ServoCommand]:
    """記録した指令のうち、サーボを動かすもの（goto / step）."""
    return [command for command in bus.commands if command.kind != "torque"]


class _StuckServoBus(FakeServoBus):
    """指令を受けても動作中のまま止まらないバス."""

    @override
    def moving(self, servo_id: int) -> bool:
        super().moving(servo_id)
        return True


class TestDegreesToSteps:
    @pytest.mark.parametrize(
        ("degrees", "steps"),
        [(0.0, 0), (90.0, 1024), (360.0, 4096), (-90.0, -1024), (5.0, 57)],
    )
    def test_converts_at_4096_steps_per_revolution(self, degrees: float, steps: int):
        assert degrees_to_steps(degrees) == steps

    def test_rounds_to_the_nearest_step(self):
        assert degrees_to_steps(0.04) == 0
        assert degrees_to_steps(0.06) == 1
        assert degrees_to_steps(-0.06) == -1


class TestScan:
    def test_returns_ids_that_answer_ping_in_the_given_order(self):
        bus = _bus()

        assert scan(bus, [3, OPEN_CLOSE, 0, ROTATION]) == (OPEN_CLOSE, ROTATION)

    def test_scans_the_whole_id_range(self):
        bus = _bus()

        assert scan(bus, range(254)) == (ROTATION, OPEN_CLOSE)

    def test_returns_empty_when_nothing_answers(self):
        assert scan(_bus(), [10, 11]) == ()


class TestWiggle:
    """判別用に ±degrees 動かして元へ戻す."""

    def test_step_mode_sends_plus_minus_and_back_after_torque_on(self):
        bus = _bus()

        wiggle(bus, ROTATION)

        assert bus.commands == [
            ServoCommand(ROTATION, "torque", 1),
            ServoCommand(ROTATION, "step", 57),
            ServoCommand(ROTATION, "step", -114),
            ServoCommand(ROTATION, "step", 57),
        ]

    def test_position_mode_moves_around_the_current_position_and_back(self):
        bus = _bus(open_close_position=2048)

        wiggle(bus, OPEN_CLOSE)

        assert bus.commands == [
            ServoCommand(OPEN_CLOSE, "torque", 1),
            ServoCommand(OPEN_CLOSE, "goto", 2105),
            ServoCommand(OPEN_CLOSE, "goto", 1991),
            ServoCommand(OPEN_CLOSE, "goto", 2048),
        ]
        assert bus.position(OPEN_CLOSE) == 2048

    def test_uses_the_given_angle(self):
        bus = _bus()

        wiggle(bus, ROTATION, 90.0)

        assert [command.value for command in _moves(bus)] == [1024, -2048, 1024]

    @pytest.mark.parametrize("position", [30, 4070])
    def test_position_mode_refuses_to_cross_the_raw_range_without_moving(
        self, position: int
    ):
        bus = _bus(open_close_position=position)

        with pytest.raises(ServoError):
            wiggle(bus, OPEN_CLOSE)

        assert _moves(bus) == []
        assert bus.position(OPEN_CLOSE) == position

    def test_position_mode_accepts_reaching_the_ends_of_the_raw_range(self):
        bus = _bus(open_close_position=57)

        wiggle(bus, OPEN_CLOSE)

        assert [command.value for command in _moves(bus)] == [114, 0, 57]

    def test_unanswered_id_is_a_servo_error(self):
        bus = _bus()

        with pytest.raises(ServoError):
            wiggle(bus, 9)

        assert bus.commands == []

    def test_servo_that_keeps_moving_times_out(self):
        bus = _StuckServoBus({ROTATION: ServoMode.STEP})

        with pytest.raises(ServoError):
            wiggle(bus, ROTATION, timeout=0.1)


class TestTweezerInit:
    def test_turns_torque_on_for_both_servos(self):
        bus = _bus()

        _tweezer(bus)

        assert sorted(bus.commands, key=lambda command: command.servo_id) == [
            ServoCommand(ROTATION, "torque", 1),
            ServoCommand(OPEN_CLOSE, "torque", 1),
        ]

    @pytest.mark.parametrize(
        ("rotation_id", "open_close_id"), [(9, OPEN_CLOSE), (ROTATION, 9)]
    )
    def test_unanswered_id_is_a_servo_error(self, rotation_id: int, open_close_id: int):
        bus = _bus()

        with pytest.raises(ServoError):
            Tweezer(
                bus,
                rotation_id=rotation_id,
                open_close_id=open_close_id,
                closed_position=1000,
                open_position=1500,
            )

    def test_rotation_not_in_step_mode_is_a_servo_error_naming_the_role(self):
        bus = FakeServoBus(
            {ROTATION: ServoMode.POSITION, OPEN_CLOSE: ServoMode.POSITION}
        )

        with pytest.raises(ServoError) as raised:
            _tweezer(bus)

        assert "rotation" in str(raised.value)

    def test_open_close_not_in_position_mode_is_a_servo_error_naming_the_role(self):
        bus = FakeServoBus({ROTATION: ServoMode.STEP, OPEN_CLOSE: ServoMode.STEP})

        with pytest.raises(ServoError) as raised:
            _tweezer(bus)

        assert "open_close" in str(raised.value)

    def test_swapped_roles_are_a_servo_error(self):
        bus = _bus()

        with pytest.raises(ServoError):
            Tweezer(
                bus,
                rotation_id=OPEN_CLOSE,
                open_close_id=ROTATION,
                closed_position=1000,
                open_position=1500,
            )


class TestTweezerRotate:
    def test_rotate_by_sends_relative_steps(self):
        bus = _bus()
        tweezer = _tweezer(bus)

        tweezer.rotate_by(90.0)
        tweezer.rotate_by(-450.0)

        assert _moves(bus) == [
            ServoCommand(ROTATION, "step", 1024),
            ServoCommand(ROTATION, "step", -5120),
        ]

    def test_rotation_below_one_step_sends_nothing(self):
        bus = _bus()
        tweezer = _tweezer(bus)

        tweezer.rotate_by(0.01)

        assert _moves(bus) == []


class TestTweezerOpen:
    """開閉は閉じ〜開きの raw 位置の間だけ動かし、開く向きを正とする."""

    def test_open_by_moves_toward_open(self):
        bus = _bus(open_close_position=1000)
        tweezer = _tweezer(bus)

        tweezer.open_by(10.0)

        assert _moves(bus) == [ServoCommand(OPEN_CLOSE, "goto", 1114)]

    def test_open_by_is_relative_to_the_current_position(self):
        bus = _bus(open_close_position=1200)
        tweezer = _tweezer(bus)

        tweezer.open_by(10.0)
        tweezer.open_by(-5.0)

        assert _moves(bus) == [
            ServoCommand(OPEN_CLOSE, "goto", 1314),
            ServoCommand(OPEN_CLOSE, "goto", 1257),
        ]

    @pytest.mark.parametrize(
        ("position", "degrees", "target"), [(1386, 10.0, 1500), (1114, -10.0, 1000)]
    )
    def test_open_by_reaches_closed_and_open_inclusive(
        self, position: int, degrees: float, target: int
    ):
        bus = _bus(open_close_position=position)
        tweezer = _tweezer(bus)

        tweezer.open_by(degrees)

        assert bus.position(OPEN_CLOSE) == target

    @pytest.mark.parametrize(("position", "degrees"), [(1450, 10.0), (1000, -10.0)])
    def test_open_by_outside_closed_to_open_is_refused_without_sending(
        self, position: int, degrees: float
    ):
        bus = _bus(open_close_position=position)
        tweezer = _tweezer(bus)

        with pytest.raises(ValueError):
            tweezer.open_by(degrees)

        assert _moves(bus) == []
        assert bus.position(OPEN_CLOSE) == position

    def test_opening_is_zero_at_closed(self):
        tweezer = _tweezer(_bus(open_close_position=1000))

        assert tweezer.opening() == 0.0

    def test_opening_is_the_angle_from_closed(self):
        bus = _bus(open_close_position=1000)
        tweezer = _tweezer(bus)

        tweezer.open_by(10.0)

        assert tweezer.opening() == pytest.approx(114 * 360 / 4096)


class TestTweezerReversed:
    """開きの raw 値が閉じより小さい機体でも、開く向きが正."""

    def test_open_by_moves_toward_open(self):
        bus = _bus(open_close_position=3000)
        tweezer = _tweezer(bus, closed_position=3000, open_position=2500)

        tweezer.open_by(10.0)

        assert _moves(bus) == [ServoCommand(OPEN_CLOSE, "goto", 2886)]
        assert tweezer.opening() == pytest.approx(114 * 360 / 4096)

    @pytest.mark.parametrize(("position", "degrees"), [(3000, -10.0), (2550, 10.0)])
    def test_open_by_outside_closed_to_open_is_refused_without_sending(
        self, position: int, degrees: float
    ):
        bus = _bus(open_close_position=position)
        tweezer = _tweezer(bus, closed_position=3000, open_position=2500)

        with pytest.raises(ValueError):
            tweezer.open_by(degrees)

        assert _moves(bus) == []


class TestOpenServoBus:
    def test_missing_port_is_a_servo_error(self, tmp_path: Path):
        # root conftest は pcbasm.hal.open_servo_bus を塞ぐので、モジュールの実物を呼ぶ。
        # 存在しないパスなので実機には届かない
        with pytest.raises(ServoError):
            servo_module.open_servo_bus(str(tmp_path / "no-such-tty"))
