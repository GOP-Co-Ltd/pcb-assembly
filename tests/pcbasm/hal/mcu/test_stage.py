"""`pcbasm.hal.mcu.stage.McuMotionStage` の仕様テスト.

疑似端末（pty）の向こうの MCU 役（``tests/support/wire_mcu.py``）と、実物の pyserial で話す。
MCU 役の軸は 80 steps/mm で、X・Y の可動域は 0〜20 mm、Z は -10〜0 mm（``tests/support/motion_mcu.py``）。
"""

import threading
from collections.abc import Iterator

import attrs
import pytest

from pcbasm.hal import MotionError, MotionState, Velocity
from pcbasm.hal.mcu.settings import McuMotionSettings
from pcbasm.hal.mcu.stage import McuMotionStage
from tests.support.motion_mcu import motion_settings, open_wire
from tests.support.polling import before_deadline, wait_until
from tests.support.wire_mcu import WireMcu

_STEP = 1 / 80  # 1 ステップ [mm]


@pytest.fixture
def stage(settings: McuMotionSettings) -> Iterator[McuMotionStage]:
    opened = McuMotionStage(settings)
    yield opened
    opened.close()


def _names(mcu: WireMcu, since: int = 0) -> list[str]:
    return [name for name, _ in mcu.accepted()[since:]]


def _enable_values(mcu: WireMcu, since: int) -> list[int]:
    return [
        params["enable"]
        for name, params in mcu.accepted()[since:]
        if name == "velocity_stream_set_enable"
    ]


class TestConnect:
    def test_starts_idle_and_not_homed(self, stage: McuMotionStage):
        status = stage.status()

        assert status.state is MotionState.IDLE
        assert status.homed == frozenset()
        assert status.shutdown_reason is None

    def test_limits_and_max_velocity_come_from_the_settings(
        self, stage: McuMotionStage
    ):
        assert (stage.limits.x.min, stage.limits.x.max) == (0.0, 20.0)
        assert (stage.limits.z.min, stage.limits.z.max) == (-10.0, 0.0)
        assert stage.limits.v.max == 100.0
        assert stage.max_velocity == Velocity(100.0, 100.0, 100.0)

    def test_fails_without_the_port(self, settings: McuMotionSettings, tmp_path):
        with pytest.raises(MotionError):
            McuMotionStage(attrs.evolve(settings, device=str(tmp_path / "no-such-tty")))


class TestSetVelocity:
    def test_rejects_an_axis_that_is_not_homed(self, stage: McuMotionStage):
        with pytest.raises(ValueError) as error:
            stage.set_velocity(Velocity(x=1.0))

        assert "原点" in str(error.value)

    def test_all_zero_while_idle_does_nothing(
        self, mcu: WireMcu, stage: McuMotionStage
    ):
        since = len(mcu.accepted())

        stage.set_velocity(Velocity())

        names = _names(mcu, since)
        assert "velocity_stream_start" not in names
        assert "velocity_stream_set_enable" not in names

    def test_moves_right_after_homing(self, mcu: WireMcu, stage: McuMotionStage):
        # 原点出しの後、MCU は接触停止の状態だが、ステージは止まっているとみなして動かせる
        stage.home(["z"])
        assert stage.status().state is MotionState.IDLE

        def moved() -> bool:
            stage.set_velocity(Velocity(z=-20.0))
            return stage.status().position.z < -1.0

        wait_until(moved, what="Z が下がること")
        assert stage.status().state is MotionState.MOVING
        stage.stop()

        status = stage.status()
        assert status.state is MotionState.IDLE
        assert status.velocity == Velocity()
        assert -10.0 <= status.position.z < -1.0

    def test_converts_mm_per_second_to_steps(self, mcu: WireMcu, stage: McuMotionStage):
        stage.home(["z"])
        since = len(mcu.accepted())

        stage.set_velocity(Velocity(z=-2.5))
        stage.stop()

        sent = [
            params["vz"]
            for name, params in mcu.accepted()[since:]
            if name == "velocity_stream_set_velocity"
        ]
        assert sent == [-200]
        # 動き出すときに励磁を入れる
        assert _enable_values(mcu, since) == [1]

    def test_other_axes_stay_rejected_after_homing_one(self, stage: McuMotionStage):
        stage.home(["z"])

        with pytest.raises(ValueError):
            stage.set_velocity(Velocity(x=1.0, z=1.0))


@pytest.mark.slow
class TestHome:
    def test_homes_every_axis_z_first(self, mcu: WireMcu, stage: McuMotionStage):
        stage.home()

        armed = [
            params["oid"]
            for name, params in mcu.accepted()
            if name == "endstop_home" and params["sample_count"] > 0
        ]
        # endstop の oid は X 4、Y 5、Z 6。1 軸につき 2 回向かう
        assert armed == [6, 6, 4, 4, 5, 5]
        status = stage.status()
        assert status.homed == frozenset({"x", "y", "z"})
        assert status.position.x == pytest.approx(0.0, abs=_STEP)
        assert status.position.y == pytest.approx(0.0, abs=_STEP)
        assert status.position.z == pytest.approx(0.0, abs=_STEP)
        assert status.state is MotionState.IDLE

    def test_homes_z_before_x_when_z_is_not_homed(self, stage: McuMotionStage):
        stage.home(["x"])

        assert stage.status().homed == frozenset({"x", "z"})


class TestMoveTo:
    def test_rejects_a_target_outside_the_limits(self, stage: McuMotionStage):
        with pytest.raises(ValueError):
            stage.move_to(z=5.0)

    def test_rejects_an_axis_that_is_not_homed(self, stage: McuMotionStage):
        with pytest.raises(ValueError) as error:
            stage.move_to(x=5.0)

        assert "原点" in str(error.value)

    def test_arrives_and_restores_the_limits(self, mcu: WireMcu, stage: McuMotionStage):
        stage.home(["z"])
        since = len(mcu.accepted())

        stage.move_to(z=-5.0)

        assert stage.status().position.z == pytest.approx(-5.0, abs=_STEP)
        limits = [
            (params["min_pos"], params["max_pos"])
            for name, params in mcu.accepted()[since:]
            if name == "velocity_stream_set_limits"
        ]
        # 向かう側の端（下限）を行き先に置き、着いたら可動域を戻す
        assert limits == [(-400, 0), (-800, 0)]
        assert stage.status().state is MotionState.IDLE

    def test_emergency_stop_gets_through_from_another_thread(
        self, mcu: WireMcu, stage: McuMotionStage
    ):
        stage.home(["z"])
        failures: list[BaseException] = []

        def move() -> None:
            try:
                stage.move_to(z=-10.0, speed=2.0)  # 5 秒かかる
            except BaseException as error:  # noqa: BLE001 - テストのスレッドへ送り直す
                failures.append(error)

        mover = threading.Thread(target=move, daemon=True)
        mover.start()
        wait_until(
            lambda: stage.status().state is MotionState.MOVING, what="動き出すこと"
        )

        before_deadline(stage.emergency_stop, what="緊急停止", deadline=2.0)
        mover.join(timeout=5.0)

        assert not mover.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], MotionError)
        wait_until(
            lambda: stage.status().state is MotionState.SHUTDOWN, what="shutdown"
        )
        assert stage.status().shutdown_reason == "Command request"


class TestShutdown:
    def test_operations_raise_after_an_emergency_stop(self, stage: McuMotionStage):
        stage.emergency_stop()
        wait_until(
            lambda: stage.status().state is MotionState.SHUTDOWN, what="shutdown"
        )

        with pytest.raises(MotionError):
            stage.relax()
        with pytest.raises(MotionError):
            stage.home(["z"])

    @pytest.mark.slow
    def test_restart_recovers(self, mcu: WireMcu, stage: McuMotionStage):
        stage.home(["z"])
        stage.emergency_stop()
        wait_until(
            lambda: stage.status().state is MotionState.SHUTDOWN, what="shutdown"
        )
        since = len(mcu.accepted())

        stage.restart()

        assert "reset" in _names(mcu, since)
        status = stage.status()
        assert status.state is MotionState.IDLE
        assert status.shutdown_reason is None
        assert status.homed == frozenset()
        stage.home(["z"])

    def test_status_raises_after_a_failed_restart(
        self, mcu: WireMcu, stage: McuMotionStage
    ):
        # 24 V を切ったので、立て直すときに TMC2209 が応えない
        mcu.control("drivers_off")

        with pytest.raises(MotionError) as error:
            stage.restart()

        assert "TMC2209" in str(error.value)
        # 閉じた前の接続の古い状態を返さない
        with pytest.raises(MotionError):
            stage.status()
        with pytest.raises(MotionError):
            stage.relax()

    def test_operations_raise_after_the_cable_is_pulled(
        self, mcu: WireMcu, stage: McuMotionStage
    ):
        mcu.unplug()

        with pytest.raises(MotionError):
            before_deadline(stage.relax, what="relax")
        with pytest.raises(MotionError):
            stage.status()


class TestRelax:
    def test_turns_the_motors_off_and_forgets_the_origin(
        self, mcu: WireMcu, stage: McuMotionStage
    ):
        stage.home(["z"])
        since = len(mcu.accepted())

        stage.relax()

        assert _enable_values(mcu, since) == [0]
        assert stage.status().homed == frozenset()
        with pytest.raises(ValueError):
            stage.set_velocity(Velocity(z=-1.0))


class TestIdleTimeout:
    """MCU 役の 1 秒を 0.1 秒に縮め、脱力までの時間を 5 秒（実時間 0.5 秒）にする.

    原点出しから次の操作までに脱力しない程度に長くしてある。
    """

    @pytest.fixture
    def quick(self) -> Iterator[McuMotionStage]:
        wire = open_wire(second=0.1)
        opened = McuMotionStage(
            attrs.evolve(motion_settings(wire.path), idle_timeout=5)
        )
        yield opened
        opened.close()
        wire.close()

    def test_forgets_the_origin_when_the_firmware_relaxes(self, quick: McuMotionStage):
        quick.home(["z"])

        wait_until(
            lambda: quick.status().homed == frozenset(),
            what="ファームが脱力して原点が「未」になること",
        )
        with pytest.raises(ValueError) as error:
            quick.set_velocity(Velocity(z=-1.0))
        assert "原点" in str(error.value)

    def test_can_home_again_after_relaxing(self, quick: McuMotionStage):
        quick.home(["z"])
        wait_until(lambda: quick.status().homed == frozenset(), what="脱力")

        quick.home(["z"])
        quick.move_to(z=-5.0)

        assert quick.status().position.z == pytest.approx(-5.0, abs=_STEP)
