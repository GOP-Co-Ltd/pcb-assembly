"""`pcbasm.hal.mcu.stage.McuMotionStage` の仕様テスト.

疑似端末（pty）の向こうの MCU 役（``tests/support/wire_mcu.py``）と、実物の pyserial で話す。
MCU 役の軸は 80 steps/mm で、X・Y の可動域は 0〜20 mm、Z は -10〜0 mm（``tests/support/motion_mcu.py``）。
"""

import logging
import threading
import time
from collections.abc import Iterator

import attrs
import pytest

from pcbasm.hal import MotionError, MotionState, Velocity
from pcbasm.hal.mcu.settings import LoadCellSettings, McuMotionSettings
from pcbasm.hal.mcu.stage import McuMotionStage
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
        params["value"]
        for name, params in mcu.accepted()[since:]
        if name == "update_digital_out"
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
        assert "update_digital_out" not in names

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
        # 動き出すときに励磁を入れる（enable_pin は反転なので 0）
        assert _enable_values(mcu, since) == [0, 0, 0]

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

        # enable_pin は反転なので、励磁を切る値は 1
        assert _enable_values(mcu, since) == [1, 1, 1]
        assert stage.status().homed == frozenset()
        with pytest.raises(ValueError):
            stage.set_velocity(Velocity(z=-1.0))


@pytest.fixture
def load_stage(settings: McuMotionSettings) -> Iterator[McuMotionStage]:
    """ロードセル（100 counts/g）を持つステージ."""
    opened = McuMotionStage(
        attrs.evolve(
            settings,
            load_cell=LoadCellSettings(
                sclk_pin="gpio0",
                dout_pin="gpio1",
                gain_channel=1,
                sample_rate=80,
                counts_per_gram=100.0,
            ),
        )
    )
    yield opened
    opened.close()


class TestLoad:
    def test_without_a_load_cell_there_is_no_load(self, stage: McuMotionStage):
        status = stage.status()

        assert (status.load, status.load_error) == (None, None)

    def test_tare_without_a_load_cell_is_rejected(self, stage: McuMotionStage):
        with pytest.raises(ValueError, match="ロードセル"):
            stage.tare()

    def test_status_reports_the_load_in_grams(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        wait_until(lambda: load_stage.status().load == 0.0, what="タール")

        mcu.control("load", counts=-250)

        wait_until(lambda: load_stage.status().load == -2.5, what="-2.5 g")

    def test_tare_zeroes_the_load(self, mcu: WireMcu, load_stage: McuMotionStage):
        wait_until(lambda: load_stage.status().load == 0.0, what="タール")

        load_stage.tare()

        assert load_stage.status().load == 0.0

    def test_moving_z_keeps_the_auto_tare_from_taking_in_the_load(
        self, mcu: WireMcu, load_stage: McuMotionStage, caplog: pytest.LogCaptureFixture
    ):
        load_stage.home(["z"])
        wait_until(lambda: load_stage.status().load == 0.0, what="タール")
        mcu.control("load", counts=100)
        end = time.monotonic() + 2.5

        def kept_moving() -> bool:
            # Z を動かしている間は、止まって安定した 1 g でもタールに取り込まない
            load_stage.set_velocity(Velocity(z=-0.5))
            assert load_stage.status().load in (0.0, 1.0)
            return time.monotonic() >= end

        wait_until(kept_moving, what="2.5 s")
        assert load_stage.status().load == 1.0

        caplog.set_level(logging.INFO, logger="pcbasm.hal.mcu.load_cell")
        load_stage.stop()
        # 止めると、動いていた区間の荷重の幅をログに出し、やがて 1 g をタールに取り込む
        wait_until(lambda: "Z が動いていた" in caplog.text, what="区間のログ")
        wait_until(lambda: load_stage.status().load == 0.0, what="自動のタール")

    def test_a_bad_sample_is_reported_instead_of_the_load(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        wait_until(lambda: load_stage.status().load == 0.0, what="タール")
        # 送り直した query_hx71x で回復しないよう、MCU 役の応答を止めてから乱す
        mcu.control("drop_blocks", count=1000)
        mcu.control("hx71x_error")

        wait_until(
            lambda: load_stage.status().load_error is not None, what="誤りの表示"
        )
        assert load_stage.status().load is None

    def test_shutdown_hides_the_load(self, mcu: WireMcu, load_stage: McuMotionStage):
        wait_until(lambda: load_stage.status().load == 0.0, what="タール")

        load_stage.emergency_stop()
        wait_until(
            lambda: load_stage.status().state is MotionState.SHUTDOWN, what="shutdown"
        )

        assert load_stage.status().load is None
        with pytest.raises(MotionError):
            load_stage.tare()
