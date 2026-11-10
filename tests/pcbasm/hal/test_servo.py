"""`pcbasm.hal.servo` の仕様テスト.

ピンセットのサーボ（Feetech STS3032 ×2）。回転はステップモード、開閉は位置モードで動かす。
バスは `FakeServoBus` を使う。`FeetechBus` は疑似端末（pty）の向こうに置いた `_WireServo` と話させ、
線に出たパケットを確かめる（scservo_sdk と pyserial は実物）。
"""

import json
import os
import subprocess
import sys
import tty
from collections.abc import Callable
from pathlib import Path

import pytest

from pcbasm.hal import (
    DEFAULT_ACCELERATION,
    DEFAULT_SPEED,
    ServoError,
    ServoMode,
    Tweezer,
    degrees_to_position,
    degrees_to_steps,
    position_to_degrees,
    scan,
    servo as servo_module,
    wait_stopped,
    wiggle,
)
from pcbasm.hal.fake import FakeServoBus, ServoCommand
from pcbasm.hal.servo import FeetechBus
from tests.support import wire_servo

ROTATION = 1
OPEN_CLOSE = 2


def _bus(open_close_position: int = 1000) -> FakeServoBus:
    """回転が ID 1（ステップモード）、開閉が ID 2（位置モード）のバス."""
    return FakeServoBus(
        {ROTATION: ServoMode.STEP, OPEN_CLOSE: ServoMode.POSITION},
        {OPEN_CLOSE: open_close_position},
    )


def _tweezer_bus(
    open_close_position: int = 1000,
    *,
    open_close_limits: tuple[int, int] = (1000, 1500),
    rotation_limits: tuple[int, int] = (0, 0),
) -> FakeServoBus:
    """サーボ設定ジョブを済ませた :func:`_bus`（Rotation は制限なし、Open Close は閉じ切り〜開き切り）."""
    return FakeServoBus(
        {ROTATION: ServoMode.STEP, OPEN_CLOSE: ServoMode.POSITION},
        {OPEN_CLOSE: open_close_position},
        angle_limits={ROTATION: rotation_limits, OPEN_CLOSE: open_close_limits},
    )


def _tweezer(
    bus: FakeServoBus,
    *,
    closed_position: int = 1000,
    open_position: int = 1500,
    rotation_speed: float = 45.0,
) -> Tweezer:
    return Tweezer(
        bus,
        rotation_id=ROTATION,
        open_close_id=OPEN_CLOSE,
        closed_position=closed_position,
        open_position=open_position,
        rotation_speed=rotation_speed,
    )


def _moves(commands: list[ServoCommand]) -> list[ServoCommand]:
    """指令のうち、サーボを動かすもの（goto / step）."""
    return [command for command in commands if command.kind in {"goto", "step"}]


def _moves_since(bus: FakeServoBus, sent: int) -> list[ServoCommand]:
    """先頭の sent 個より後に受けた goto / step."""
    return _moves(bus.commands[sent:])


_INSTRUCTION_WRITE = 0x03
WIRE_SERVO_SCRIPT = Path(wire_servo.__file__)
_BROADCAST = 0xFE


class _WireServo:
    """疑似端末の master 側で STS のサーボを演じる（受けた instruction packet を記録し、status packet
    を返す）.

    演じるのは別プロセス（``tests/support/wire_servo.py``）。読み出しには ``registers``（先頭アドレス → バイト列）
    にあればその値を、無ければ 0 を並べて返し、ブロードキャストには応答しない。slave 側のパス（:attr:`path`）をポートとして開かせる。``reply_delays`` にあるアドレスへの
    WRITE は、その秒数だけ遅れて応答する。
    """

    def __init__(
        self,
        reply_delays: dict[int, float] | None = None,
        registers: dict[int, bytes] | None = None,
    ) -> None:
        self._master, self._slave = os.openpty()
        tty.setraw(self._master)
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(WIRE_SERVO_SCRIPT),
                str(self._master),
                json.dumps(reply_delays or {}),
                json.dumps(
                    {key: value.hex() for key, value in (registers or {}).items()}
                ),
            ],
            pass_fds=(self._master,),
            stdout=subprocess.PIPE,
        )
        assert self._process.stdout is not None
        self._stdout = self._process.stdout.fileno()
        # 子が読み始める前に送ると、最初の応答が SDK の待ち時間を超える
        assert self._process.stdout.readline() == b"ready\n"
        os.set_blocking(self._stdout, False)
        self._received = b""
        self._packets: list[bytes] = []
        self._unplugged = False

    @property
    def packets(self) -> list[bytes]:
        """受けた instruction packet（受けた順）."""
        while True:
            try:
                chunk = os.read(self._stdout, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            self._received += chunk
        *lines, self._received = self._received.split(b"\n")
        self._packets += [bytes.fromhex(line.decode()) for line in lines]
        return list(self._packets)

    def writes(self) -> list[tuple[int, int, bytes]]:
        """受けた WRITE の（ID、先頭アドレス、データ）."""
        return [
            (packet[2], packet[5], packet[6:-1])
            for packet in self.packets
            if packet[4] == _INSTRUCTION_WRITE
        ]

    def unplug(self) -> None:
        """線を抜いたことにする（master を閉じ、slave 側の I/O を失敗させる）."""
        self._unplugged = True
        self._process.kill()
        self._process.wait()
        os.close(self._master)

    def close(self) -> None:
        if not self._unplugged:
            self.unplug()
        assert self._process.stdout is not None
        self._process.stdout.close()
        os.close(self._slave)


@pytest.fixture
def wire():
    servo = _WireServo()
    yield servo
    servo.close()


@pytest.fixture
def feetech(wire: _WireServo):
    # root conftest は pcbasm.hal.open_servo_bus を塞ぐので、モジュールの実物を呼ぶ。
    # 開くのはテストが作った疑似端末なので実機には届かない
    bus = servo_module.open_servo_bus(wire.path)
    yield bus
    bus.close()


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

    @pytest.mark.parametrize(
        ("half_steps", "steps"), [(1, 1), (5, 3), (-1, -1), (-5, -3)]
    )
    def test_rounds_half_away_from_zero(self, half_steps: int, steps: int):
        degrees = half_steps / 2 * 360 / 4096  # ちょうど x.5 step（2 進で割り切れる）

        assert degrees_to_steps(degrees) == steps


class TestAbsoluteAngle:
    """位置（raw 0〜4095）とサーボの絶対角 [deg] の換算."""

    @pytest.mark.parametrize(
        ("position", "degrees"),
        [(0, 0.0), (1024, 90.0), (2048, 180.0), (4095, 359.912109375)],
    )
    def test_position_to_degrees(self, position: int, degrees: float):
        assert position_to_degrees(position) == degrees

    @pytest.mark.parametrize("position", [0, 1, 2004, 2345, 4095])
    def test_two_decimal_degrees_round_trip_to_the_same_position(self, position: int):
        assert degrees_to_position(round(position_to_degrees(position), 2)) == position

    def test_full_turn_wraps_to_zero(self):
        assert degrees_to_position(359.99) == 0


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
            ServoCommand(OPEN_CLOSE, "goto", 2048),
            ServoCommand(OPEN_CLOSE, "torque", 1),
            ServoCommand(OPEN_CLOSE, "goto", 2105),
            ServoCommand(OPEN_CLOSE, "goto", 1991),
            ServoCommand(OPEN_CLOSE, "goto", 2048),
        ]
        assert bus.position(OPEN_CLOSE) == 2048

    def test_uses_the_given_angle(self):
        bus = _bus()

        wiggle(bus, ROTATION, 90.0)

        assert [command.value for command in _moves(bus.commands)] == [
            1024,
            -2048,
            1024,
        ]

    @pytest.mark.parametrize("position", [30, 4070])
    def test_position_mode_refuses_to_cross_the_raw_range_without_moving(
        self, position: int
    ):
        bus = _bus(open_close_position=position)

        with pytest.raises(ServoError):
            wiggle(bus, OPEN_CLOSE)

        assert bus.commands == []
        assert bus.position(OPEN_CLOSE) == position

    def test_position_mode_accepts_reaching_the_ends_of_the_raw_range(self):
        bus = _bus(open_close_position=57)

        wiggle(bus, OPEN_CLOSE)

        # 先頭の 57 はトルクを入れる前に goal を今の位置にしたもの
        assert [command.value for command in _moves(bus.commands)] == [57, 114, 0, 57]

    def test_unanswered_id_is_a_servo_error(self):
        bus = _bus()

        with pytest.raises(ServoError):
            wiggle(bus, 9)

        assert bus.commands == []

    @pytest.mark.parametrize("servo_id", [ROTATION, OPEN_CLOSE])
    def test_servo_that_keeps_moving_times_out_with_the_torque_off(self, servo_id: int):
        bus = _bus(open_close_position=2048)
        bus.jam(servo_id)

        with pytest.raises(ServoError):
            wiggle(bus, servo_id, timeout=0.1)

        assert bus.commands[-1] == ServoCommand(servo_id, "torque", 0)


class TestWaitStopped:
    """指令の後、動き出して止まるまで待つ（指令の直後は実機でもまだ moving が立たない）."""

    def test_waits_for_a_servo_that_starts_moving_late(self):
        bus = FakeServoBus({OPEN_CLOSE: ServoMode.POSITION}, start_delay=3)
        bus.goto(OPEN_CLOSE, 2100)

        assert wait_stopped(bus, OPEN_CLOSE, timeout=1.0)
        # 動き出し → 停止まで読み切っている（後から動き出すことはない）
        assert not any(bus.moving(OPEN_CLOSE) for _ in range(5))

    def test_returns_when_the_servo_never_starts(self):
        bus = FakeServoBus({OPEN_CLOSE: ServoMode.POSITION})

        assert wait_stopped(bus, OPEN_CLOSE, timeout=1.0)

    def test_servo_that_keeps_moving_times_out(self):
        bus = FakeServoBus({OPEN_CLOSE: ServoMode.POSITION})
        bus.jam(OPEN_CLOSE)

        assert not wait_stopped(bus, OPEN_CLOSE, timeout=0.1)


class TestTweezerTorque:
    """指令ごとに、動かすサーボだけにトルクを入れる（もう一方の goal とトルクには触れない）."""

    def test_rotate_by_turns_on_only_the_rotation(self):
        bus = _tweezer_bus()

        _tweezer(bus).rotate_by(90.0)

        assert bus.commands == [
            ServoCommand(ROTATION, "torque", 1),
            ServoCommand(ROTATION, "step", 1024),
        ]

    @pytest.mark.parametrize(
        ("command", "target"),
        [(lambda t: t.open_by(10.0), 1114), (lambda t: t.open_to(0.5), 1250)],
        ids=["open_by", "open_to"],
    )
    def test_open_writes_the_goal_before_the_torque(
        self, command: Callable[[Tweezer], None], target: int
    ):
        """Goal を先に書くので、トルクを入れた瞬間に古い goal へ跳ばない."""
        bus = _tweezer_bus(open_close_position=1000)

        command(_tweezer(bus))

        assert bus.commands == [
            ServoCommand(OPEN_CLOSE, "goto", target),
            ServoCommand(OPEN_CLOSE, "torque", 1),
        ]

    def test_rotating_while_gripping_leaves_the_open_close_goal(self):
        """つまんだまま回しても Open Close の goal を今の位置で上書きしない（把持力が抜けない）.

        Web の API は指令のたびにピンセットを組み直すので、組み直した後で確かめる。
        """
        bus = _tweezer_bus(open_close_position=1200)
        _tweezer(bus).open_to(0.0)
        sent = len(bus.commands)

        _tweezer(bus).rotate_by(10.0)

        assert [c for c in bus.commands[sent:] if c.servo_id == OPEN_CLOSE] == []

    def test_relaxed_servo_gets_torque_again_on_the_next_command(self):
        bus = _tweezer_bus()
        tweezer = _tweezer(bus)
        tweezer.rotate_by(90.0)
        bus.release_all()

        tweezer.rotate_by(90.0)

        assert bus.commands[-2:] == [
            ServoCommand(ROTATION, "torque", 1),
            ServoCommand(ROTATION, "step", 1024),
        ]


class TestTweezerInit:
    def test_makes_no_moves_and_leaves_the_torque_alone(self):
        """組むだけではトルクを入れない（パネルの現在値の取得で、脱力したサーボを保持しない）."""
        bus = _tweezer_bus()

        _tweezer(bus)

        assert bus.commands == []

    @pytest.mark.parametrize(
        ("rotation_id", "open_close_id"), [(9, OPEN_CLOSE), (ROTATION, 9)]
    )
    def test_unanswered_id_is_a_servo_error(self, rotation_id: int, open_close_id: int):
        bus = _tweezer_bus()

        with pytest.raises(ServoError):
            Tweezer(
                bus,
                rotation_id=rotation_id,
                open_close_id=open_close_id,
                closed_position=1000,
                open_position=1500,
                rotation_speed=45.0,
            )

    @pytest.mark.parametrize("limits", [(0, 4095), (0, 1)])
    def test_rotation_with_angle_limits_is_a_servo_error_asking_for_the_setup(
        self, limits: tuple[int, int]
    ):
        """ステップモードでも角度制限が効き、同じ向きに回し続けると端で止まる."""
        bus = _tweezer_bus(rotation_limits=limits)

        with pytest.raises(ServoError) as raised:
            _tweezer(bus)

        assert "rotation" in str(raised.value)
        assert "サーボ設定" in str(raised.value)

    @pytest.mark.parametrize("limits", [(0, 4095), (1000, 1501), (999, 1500)])
    def test_open_close_limits_other_than_closed_to_open_are_a_servo_error(
        self, limits: tuple[int, int]
    ):
        """開き切りの先へ回さない守り（サーボ自身の角度制限）が外れたままなら組まない."""
        bus = _tweezer_bus(open_close_limits=limits)

        with pytest.raises(ServoError) as raised:
            _tweezer(bus)

        assert "open_close" in str(raised.value)
        assert "サーボ設定" in str(raised.value)
        assert bus.commands == []

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
        bus = _tweezer_bus()

        with pytest.raises(ServoError):
            Tweezer(
                bus,
                rotation_id=OPEN_CLOSE,
                open_close_id=ROTATION,
                closed_position=1000,
                open_position=1500,
                rotation_speed=45.0,
            )


class TestTweezerRotate:
    def test_rotate_by_sends_relative_steps(self):
        bus = _tweezer_bus()
        tweezer = _tweezer(bus)
        sent = len(bus.commands)

        tweezer.rotate_by(90.0)
        tweezer.rotate_by(-450.0)

        assert _moves_since(bus, sent) == [
            ServoCommand(ROTATION, "step", 1024),
            ServoCommand(ROTATION, "step", -5120),
        ]

    def test_rotation_below_one_step_sends_nothing(self):
        bus = _tweezer_bus()
        tweezer = _tweezer(bus)
        sent = len(bus.commands)

        tweezer.rotate_by(0.01)

        assert _moves_since(bus, sent) == []

    def test_rotation_accelerates_fast_to_stop_short(self):
        bus = _tweezer_bus()

        _tweezer(bus).rotate_by(90.0)

        # 止めるときの行き過ぎを小さくするため速く加減速する
        assert bus.acceleration(ROTATION) == 50

    @pytest.mark.parametrize(("dps", "speed"), [(45.0, 512), (90.0, 1024), (1.0, 11)])
    def test_rotate_by_turns_at_the_rotation_speed(self, dps: float, speed: int):
        bus = _tweezer_bus()
        tweezer = _tweezer(bus, rotation_speed=dps)

        tweezer.rotate_by(90.0)

        assert bus.speed(ROTATION) == speed


class TestTweezerKeepRotating:
    """押している間の連続回転（既定の 45°/s = 512 step/s、加速度 5000 step/s²）.

    - 減速距離: 27 step
    - 送り足す下限: 減速距離 + 0.1 秒分（51）= 78 step
    - 送り足す上限: 減速距離 + 0.3 秒分（153）= 180 step
    """

    @pytest.mark.parametrize("direction", [1, -1])
    def test_starts_with_the_remaining_up_to_the_ceiling(self, direction: int):
        bus = _tweezer_bus()

        _tweezer(bus).keep_rotating(direction)

        assert bus.remaining_steps(ROTATION) == direction * 180
        assert bus.commands[0] == ServoCommand(ROTATION, "torque", 1)
        assert (bus.speed(ROTATION), bus.acceleration(ROTATION)) == (512, 50)

    def test_tops_up_only_when_the_remaining_falls_below_the_floor(self):
        bus = _tweezer_bus()
        tweezer = _tweezer(bus)
        tweezer.keep_rotating(1)

        bus.advance(ROTATION, 180 - 78)  # 残り 78: まだ足さない
        sent = len(bus.commands)
        tweezer.keep_rotating(1)
        assert len(bus.commands) == sent

        bus.advance(ROTATION, 1)  # 残り 77: 上限まで足す
        tweezer.keep_rotating(1)
        assert bus.commands[-1] == ServoCommand(ROTATION, "step", 180 - 77)
        assert bus.remaining_steps(ROTATION) == 180

    def test_reversing_sends_the_ceiling_the_other_way(self):
        bus = _tweezer_bus()
        tweezer = _tweezer(bus)
        tweezer.keep_rotating(1)

        tweezer.keep_rotating(-1)

        assert bus.remaining_steps(ROTATION) == -180

    def test_scales_with_the_rotation_speed(self):
        """90°/s = 1024 step/s: 減速距離 105、上限 105 + 307 = 412."""
        bus = _tweezer_bus()

        _tweezer(bus, rotation_speed=90.0).keep_rotating(1)

        assert bus.remaining_steps(ROTATION) == 412

    def test_reversing_a_long_remaining_sends_at_most_15_bits(self):
        """パネルで大きく回している途中に逆向きを押しても、1 回に送れる step を超えない."""
        bus = _tweezer_bus()
        tweezer = _tweezer(bus)
        bus.step(ROTATION, 32700)

        tweezer.keep_rotating(-1)

        assert bus.commands[-1] == ServoCommand(ROTATION, "step", -32767)

    @pytest.mark.parametrize("direction", [0, 2, -2])
    def test_direction_other_than_plus_or_minus_one_is_refused(self, direction: int):
        bus = _tweezer_bus()

        with pytest.raises(ValueError):
            _tweezer(bus).keep_rotating(direction)

        assert bus.commands == []


class TestTweezerStopRotation:
    """止めるときは、減速距離（27 step）だけ残す.

    残りを 0 にすると行き過ぎて戻る.
    """

    @pytest.mark.parametrize("direction", [1, -1])
    def test_leaves_only_the_stopping_distance(self, direction: int):
        bus = _tweezer_bus()
        tweezer = _tweezer(bus)
        tweezer.keep_rotating(direction)

        tweezer.stop_rotation()

        assert bus.commands[-1] == ServoCommand(ROTATION, "step", -direction * 153)
        assert bus.remaining_steps(ROTATION) == direction * 27

    @pytest.mark.parametrize("remaining", [27, 0, -10])
    def test_sends_nothing_within_the_stopping_distance(self, remaining: int):
        bus = _tweezer_bus()
        tweezer = _tweezer(bus)
        if remaining:
            bus.step(ROTATION, remaining)
        sent = len(bus.commands)

        tweezer.stop_rotation()

        assert len(bus.commands) == sent


class TestTweezerOpen:
    """開閉は閉じ〜開きの raw 位置の間だけ動かし、開く向きを正とする."""

    def test_open_by_moves_toward_open(self):
        bus = _tweezer_bus(open_close_position=1000)
        tweezer = _tweezer(bus)
        sent = len(bus.commands)

        tweezer.open_by(10.0)

        assert _moves_since(bus, sent) == [ServoCommand(OPEN_CLOSE, "goto", 1114)]

    def test_open_by_is_relative_to_the_current_position(self):
        bus = _tweezer_bus(open_close_position=1200)
        tweezer = _tweezer(bus)
        sent = len(bus.commands)

        tweezer.open_by(10.0)
        tweezer.open_by(-5.0)

        assert _moves_since(bus, sent) == [
            ServoCommand(OPEN_CLOSE, "goto", 1314),
            ServoCommand(OPEN_CLOSE, "goto", 1257),
        ]

    @pytest.mark.parametrize(
        ("position", "degrees", "target"), [(1386, 10.0, 1500), (1114, -10.0, 1000)]
    )
    def test_open_by_reaches_closed_and_open_inclusive(
        self, position: int, degrees: float, target: int
    ):
        bus = _tweezer_bus(open_close_position=position)
        tweezer = _tweezer(bus)

        tweezer.open_by(degrees)

        assert bus.position(OPEN_CLOSE) == target

    @pytest.mark.parametrize(("position", "degrees"), [(1450, 10.0), (1000, -10.0)])
    def test_open_by_outside_closed_to_open_is_refused_without_sending(
        self, position: int, degrees: float
    ):
        bus = _tweezer_bus(open_close_position=position)
        tweezer = _tweezer(bus)
        sent = len(bus.commands)

        with pytest.raises(ValueError):
            tweezer.open_by(degrees)

        assert _moves_since(bus, sent) == []
        assert bus.position(OPEN_CLOSE) == position

    def test_opening_is_zero_at_closed(self):
        tweezer = _tweezer(_tweezer_bus(open_close_position=1000))

        assert tweezer.opening() == 0.0

    def test_opening_is_the_angle_from_closed(self):
        bus = _tweezer_bus(open_close_position=1000)
        tweezer = _tweezer(bus)

        tweezer.open_by(10.0)

        assert tweezer.opening() == pytest.approx(114 * 360 / 4096)


class TestTweezerOpenTo:
    """開き具合 0〜1（0=閉じ切り、1=開き切り）の絶対指令."""

    @pytest.mark.parametrize(
        ("fraction", "target"), [(0.0, 1000), (0.5, 1250), (1.0, 1500), (0.333, 1166)]
    )
    def test_open_to_goes_to_the_fraction_between_closed_and_open(
        self, fraction: float, target: int
    ):
        bus = _tweezer_bus(open_close_position=1200)
        tweezer = _tweezer(bus)
        sent = len(bus.commands)

        tweezer.open_to(fraction)

        assert _moves_since(bus, sent) == [ServoCommand(OPEN_CLOSE, "goto", target)]

    @pytest.mark.parametrize("fraction", [-0.01, 1.01, float("nan")])
    def test_open_to_outside_0_to_1_is_refused_without_sending(self, fraction: float):
        bus = _tweezer_bus(open_close_position=1200)
        tweezer = _tweezer(bus)

        with pytest.raises(ValueError):
            tweezer.open_to(fraction)

        assert bus.commands == []

    @pytest.mark.parametrize(
        ("position", "fraction"), [(1000, 0.0), (1250, 0.5), (1500, 1.0)]
    )
    def test_open_fraction_is_the_place_between_closed_and_open(
        self, position: int, fraction: float
    ):
        tweezer = _tweezer(_tweezer_bus(open_close_position=position))

        assert tweezer.open_fraction() == pytest.approx(fraction)

    @pytest.mark.parametrize(
        "command",
        [lambda t: t.open_to(1.0), lambda t: t.open_by(10.0)],
        ids=["open_to", "open_by"],
    )
    def test_open_close_moves_fast_to_follow_the_trigger(
        self, command: Callable[[Tweezer], None]
    ):
        """コントローラの LT は 50 Hz で目標を変える.

        既定（500 step/s、加速度 10）では約 0.5 秒遅れる.
        """
        bus = _tweezer_bus()

        command(_tweezer(bus, rotation_speed=90.0))

        assert (bus.speed(OPEN_CLOSE), bus.acceleration(OPEN_CLOSE)) == (2000, 100)

    def test_target_fraction_is_the_goal_even_when_the_position_differs(self):
        """つまんで閉じ切りの手前で止まっていても、目標は送った開き具合のまま."""
        bus = _tweezer_bus(open_close_position=1200)
        tweezer = _tweezer(bus)
        tweezer.open_to(0.0)
        bus.set_torque(OPEN_CLOSE, False)
        bus.turn_by_hand(OPEN_CLOSE, 1250)

        assert tweezer.target_fraction() == 0.0
        assert tweezer.open_fraction() == pytest.approx(0.5)

    def test_open_fraction_reads_without_sending(self):
        bus = _tweezer_bus(open_close_position=1250)

        _tweezer(bus).open_fraction()

        assert bus.commands == []


class TestTweezerReversed:
    """開きの raw 値が閉じより小さい機体でも、開く向きが正."""

    def test_open_by_moves_toward_open(self):
        bus = _tweezer_bus(open_close_position=3000, open_close_limits=(2500, 3000))
        tweezer = _tweezer(bus, closed_position=3000, open_position=2500)
        sent = len(bus.commands)

        tweezer.open_by(10.0)

        assert _moves_since(bus, sent) == [ServoCommand(OPEN_CLOSE, "goto", 2886)]
        assert tweezer.opening() == pytest.approx(114 * 360 / 4096)

    @pytest.mark.parametrize(
        ("fraction", "target"), [(0.0, 3000), (0.2, 2900), (1.0, 2500)]
    )
    def test_open_to_and_open_fraction(self, fraction: float, target: int):
        bus = _tweezer_bus(open_close_position=2700, open_close_limits=(2500, 3000))
        tweezer = _tweezer(bus, closed_position=3000, open_position=2500)

        tweezer.open_to(fraction)

        assert bus.position(OPEN_CLOSE) == target
        assert tweezer.open_fraction() == pytest.approx(fraction)

    @pytest.mark.parametrize(("position", "degrees"), [(3000, -10.0), (2550, 10.0)])
    def test_open_by_outside_closed_to_open_is_refused_without_sending(
        self, position: int, degrees: float
    ):
        bus = _tweezer_bus(open_close_position=position, open_close_limits=(2500, 3000))
        tweezer = _tweezer(bus, closed_position=3000, open_position=2500)
        sent = len(bus.commands)

        with pytest.raises(ValueError):
            tweezer.open_by(degrees)

        assert _moves_since(bus, sent) == []


class TestOpenServoBus:
    def test_missing_port_is_a_servo_error(self, tmp_path: Path):
        # root conftest は pcbasm.hal.open_servo_bus を塞ぐので、モジュールの実物を呼ぶ。
        # 存在しないパスなので実機には届かない
        with pytest.raises(ServoError):
            servo_module.open_servo_bus(str(tmp_path / "no-such-tty"))


def _goal(goal_low: int, goal_high: int, speed: int = 500) -> bytes:
    """ACC から 7 byte（ACC=10、goal、time=0、speed は既定 500）."""
    return bytes([10, goal_low, goal_high, 0, 0, speed & 0xFF, speed >> 8])


_ADDR_ACC = 41
_INSTRUCTION_READ = 2


def _reads(wire: _WireServo) -> list[tuple[int, int, int]]:
    """受けた READ の（ID、先頭アドレス、バイト数）."""
    return [
        (packet[2], packet[5], packet[6])
        for packet in wire.packets
        if packet[4] == _INSTRUCTION_READ
    ]


class TestFeetechBus:
    """線に出る STS のパケット（ACC から 7 byte を 1 回の WRITE で送る）."""

    @pytest.mark.parametrize(
        ("steps", "goal"),
        [
            (100, _goal(0x64, 0x00)),
            (-100, _goal(0x64, 0x80)),  # 負は bit15 を立てた sign-magnitude
            (32767, _goal(0xFF, 0x7F)),
            (-32767, _goal(0xFF, 0xFF)),
        ],
    )
    def test_step_writes_sign_magnitude_goal(
        self, wire: _WireServo, feetech: FeetechBus, steps: int, goal: bytes
    ):
        feetech.step(1, steps)

        assert wire.writes() == [(1, _ADDR_ACC, goal)]

    @pytest.mark.parametrize(
        ("position", "goal"),
        [(0, _goal(0x00, 0x00)), (1234, _goal(0xD2, 0x04)), (4095, _goal(0xFF, 0x0F))],
    )
    def test_goto_writes_acc_position_time_and_speed(
        self, wire: _WireServo, feetech: FeetechBus, position: int, goal: bytes
    ):
        feetech.goto(2, position)

        assert wire.writes() == [(2, _ADDR_ACC, goal)]

    @pytest.mark.parametrize("speed", [1, 1024, 32767])
    def test_step_and_goto_write_the_given_speed(
        self, wire: _WireServo, feetech: FeetechBus, speed: int
    ):
        feetech.step(1, 100, speed=speed)
        feetech.goto(2, 1234, speed=speed)

        assert wire.writes() == [
            (1, _ADDR_ACC, _goal(0x64, 0x00, speed)),
            (2, _ADDR_ACC, _goal(0xD2, 0x04, speed)),
        ]

    def test_step_writes_the_given_acceleration(
        self, wire: _WireServo, feetech: FeetechBus
    ):
        feetech.step(1, 100, acceleration=50)

        assert wire.writes() == [(1, _ADDR_ACC, bytes([50]) + _goal(0x64, 0x00)[1:])]

    def test_goto_writes_the_given_acceleration(
        self, wire: _WireServo, feetech: FeetechBus
    ):
        feetech.goto(2, 1234, acceleration=100)

        assert wire.writes() == [(2, _ADDR_ACC, bytes([100]) + _goal(0xD2, 0x04)[1:])]

    @pytest.mark.parametrize("acceleration", [0, 255])
    def test_acceleration_outside_1_to_254_is_refused_without_sending(
        self, wire: _WireServo, feetech: FeetechBus, acceleration: int
    ):
        with pytest.raises(ValueError):
            feetech.step(1, 100, acceleration=acceleration)
        with pytest.raises(ValueError):
            feetech.goto(2, 1234, acceleration=acceleration)

        assert wire.packets == []

    def test_remaining_steps_reads_the_present_position(
        self, wire: _WireServo, feetech: FeetechBus
    ):
        assert feetech.remaining_steps(1) == 0  # 疑似サーボは読み出しに 0 を返す

        assert _reads(wire) == [(1, 56, 2)]

    @pytest.mark.parametrize(
        ("raw", "remaining"),
        [(b"\x2c\x01", 300), (b"\x2c\x81", -300), (b"\xff\xff", -32767)],
    )
    def test_remaining_steps_decodes_sign_magnitude(self, raw: bytes, remaining: int):
        """負の残りは bit15 が立つ（読み違えると、送り足しと停止が残りを膨らませる）."""
        wire = _WireServo(registers={56: raw})
        bus = servo_module.open_servo_bus(wire.path)
        try:
            assert bus.remaining_steps(1) == remaining
        finally:
            bus.close()
            wire.close()

    @pytest.mark.parametrize("speed", [0, -1, 32768])
    def test_speed_outside_1_to_32767_is_refused_without_sending(
        self, wire: _WireServo, feetech: FeetechBus, speed: int
    ):
        with pytest.raises(ValueError):
            feetech.step(1, 100, speed=speed)
        with pytest.raises(ValueError):
            feetech.goto(2, 1234, speed=speed)

        assert wire.packets == []

    def test_goal_reads_two_bytes_at_the_goal_position(
        self, wire: _WireServo, feetech: FeetechBus
    ):
        assert feetech.goal(2) == 0  # 疑似サーボは読み出しに 0 を返す

        assert _reads(wire) == [(2, 42, 2)]

    def test_angle_limits_reads_min_and_max(
        self, wire: _WireServo, feetech: FeetechBus
    ):
        assert feetech.angle_limits(1) == (0, 0)

        assert _reads(wire) == [(1, 9, 2), (1, 11, 2)]

    def test_clear_angle_limits_writes_zero_and_zero_inside_the_eeprom_lock(
        self, wire: _WireServo, feetech: FeetechBus
    ):
        feetech.clear_angle_limits(1)

        assert wire.writes() == [
            (1, 55, b"\x00"),
            (1, 9, b"\x00\x00"),
            (1, 11, b"\x00\x00"),
            (1, 55, b"\x01"),
        ]

    @pytest.mark.parametrize("mode", list(ServoMode))
    def test_set_mode_unlocks_the_eeprom_writes_the_mode_and_locks_it_again(
        self, wire: _WireServo, feetech: FeetechBus, mode: ServoMode
    ):
        feetech.set_mode(1, mode)

        assert wire.writes() == [
            (1, 55, b"\x00"),
            (1, 33, bytes([mode.value])),
            (1, 55, b"\x01"),
        ]

    def test_set_angle_limits_writes_min_and_max_inside_the_eeprom_lock(
        self, wire: _WireServo, feetech: FeetechBus
    ):
        feetech.set_angle_limits(2, 2004, 2345)

        assert wire.writes() == [
            (2, 55, b"\x00"),
            (2, 9, (2004).to_bytes(2, "little")),
            (2, 11, (2345).to_bytes(2, "little")),
            (2, 55, b"\x01"),
        ]

    @pytest.mark.parametrize(
        ("low", "high"), [(100, 100), (200, 100), (-1, 100), (0, 4096)]
    )
    def test_unusable_angle_limits_are_refused_without_sending(
        self, wire: _WireServo, feetech: FeetechBus, low: int, high: int
    ):
        with pytest.raises(ValueError):
            feetech.set_angle_limits(2, low, high)

        assert wire.packets == []

    def test_release_all_broadcasts_torque_off_without_waiting_for_a_reply(
        self, wire: _WireServo, feetech: FeetechBus
    ):
        feetech.release_all()
        feetech.set_torque(1, True)  # 応答の無い送信の後も、次のやり取りが乱れない

        assert wire.writes() == [(_BROADCAST, 40, b"\x00"), (1, 40, b"\x01")]

    def test_set_mode_waits_for_the_late_reply_of_a_mode_change(self):
        # 実機は MODE が変わると応答まで約 40 ms かかる
        wire = _WireServo(reply_delays={33: 0.08})
        bus = servo_module.open_servo_bus(wire.path)
        try:
            bus.set_mode(1, ServoMode.STEP)

            # 遅れた応答を読み切っているので、次の読み出しが乱れない
            assert bus.mode(1) is ServoMode.POSITION  # _WireServo は 0 を返す
            assert [address for _, address, _ in wire.writes()] == [55, 33, 55]
        finally:
            bus.close()
            wire.close()

    @pytest.mark.parametrize("steps", [32768, -32768])
    def test_step_beyond_15_bits_is_refused_without_sending(
        self, wire: _WireServo, feetech: FeetechBus, steps: int
    ):
        with pytest.raises(ValueError):
            feetech.step(1, steps)

        assert wire.packets == []

    @pytest.mark.parametrize("position", [-1, 4096])
    def test_goto_outside_the_raw_range_is_refused_without_sending(
        self, wire: _WireServo, feetech: FeetechBus, position: int
    ):
        with pytest.raises(ValueError):
            feetech.goto(2, position)

        assert wire.packets == []

    def test_unplugged_port_is_a_servo_error(
        self, wire: _WireServo, feetech: FeetechBus
    ):
        wire.unplug()

        with pytest.raises(ServoError):
            feetech.step(1, 100)
        with pytest.raises(ServoError):
            feetech.position(2)

    def test_call_after_close_is_a_servo_error_without_reopening(
        self, wire: _WireServo, feetech: FeetechBus
    ):
        feetech.close()

        with pytest.raises(ServoError):
            feetech.step(1, 100)

        assert wire.packets == []

    def test_recovers_on_the_next_call_after_replugging(self, tmp_path: Path):
        # udev のシンボリックリンクのように、同じパスの先が差し直したポートに替わる
        link = tmp_path / "ttyServo"
        first, second = _WireServo(), _WireServo()
        try:
            link.symlink_to(first.path)
            bus = servo_module.open_servo_bus(str(link))
            first.unplug()
            with pytest.raises(ServoError):
                bus.step(1, 100)

            link.unlink()
            link.symlink_to(second.path)
            bus.step(1, 100)

            assert second.writes() == [(1, _ADDR_ACC, _goal(0x64, 0x00))]
            bus.close()
        finally:
            first.close()
            second.close()
