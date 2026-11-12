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

from pcbasm.hal import ContactStopped, MotionError, MotionState, Velocity
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
        wait_until(lambda: load_stage.status().load == 0.0, what="風袋引き")

        mcu.control("load", counts=-250)

        wait_until(lambda: load_stage.status().load == -2.5, what="-2.5 g")

    def test_tare_zeroes_the_load(self, mcu: WireMcu, load_stage: McuMotionStage):
        wait_until(lambda: load_stage.status().load == 0.0, what="風袋引き")

        load_stage.tare()

        assert load_stage.status().load == 0.0

    def test_moving_z_keeps_the_auto_tare_from_taking_in_the_load(
        self, mcu: WireMcu, load_stage: McuMotionStage, caplog: pytest.LogCaptureFixture
    ):
        # 風袋を取るまでは接触停止を張れないので動かさない
        wait_until(lambda: load_stage.status().load == 0.0, what="風袋引き")
        load_stage.home(["z"])
        mcu.control("load", counts=100)
        end = time.monotonic() + 2.5

        def kept_moving() -> bool:
            # Z を動かしている間は、止まって安定した 1 g でも風袋に取り込まない
            load_stage.set_velocity(Velocity(z=-0.5))
            assert load_stage.status().load in (0.0, 1.0)
            return time.monotonic() >= end

        wait_until(kept_moving, what="2.5 s")
        assert load_stage.status().load == 1.0

        caplog.set_level(logging.INFO, logger="pcbasm.hal.mcu.load_cell")
        load_stage.stop()
        # 止めると、動いていた区間の荷重の幅をログに出し、やがて 1 g を風袋に取り込む
        wait_until(lambda: "Z が動いていた" in caplog.text, what="区間のログ")
        wait_until(lambda: load_stage.status().load == 0.0, what="自動の風袋引き")

    def test_suspended_auto_tare_keeps_a_quiet_load_until_resumed(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        wait_until(lambda: load_stage.status().load == 0.0, what="風袋引き")
        load_stage.suspend_auto_tare(True)
        mcu.control("load", counts=100)
        end = time.monotonic() + 2.5

        def kept() -> bool:
            # Z が止まって安定した 1 g でも、止めている間は風袋に取り込まない
            assert load_stage.status().load in (0.0, 1.0)
            return time.monotonic() >= end

        wait_until(kept, what="2.5 s")
        assert load_stage.status().load == 1.0

        load_stage.suspend_auto_tare(False)
        wait_until(lambda: load_stage.status().load == 0.0, what="自動の風袋引き")

    def test_suspend_auto_tare_without_a_load_cell_does_nothing(
        self, stage: McuMotionStage
    ):
        stage.suspend_auto_tare(True)
        stage.suspend_auto_tare(False)

    def test_a_bad_sample_is_reported_instead_of_the_load(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        wait_until(lambda: load_stage.status().load == 0.0, what="風袋引き")
        # 送り直した query_hx71x で回復しないよう、MCU 役の応答を止めてから乱す
        mcu.control("drop_blocks", count=1000)
        mcu.control("hx71x_error")

        wait_until(
            lambda: load_stage.status().load_error is not None, what="誤りの表示"
        )
        assert load_stage.status().load is None

    def test_shutdown_hides_the_load(self, mcu: WireMcu, load_stage: McuMotionStage):
        wait_until(lambda: load_stage.status().load == 0.0, what="風袋引き")

        load_stage.emergency_stop()
        wait_until(
            lambda: load_stage.status().state is MotionState.SHUTDOWN, what="shutdown"
        )

        assert load_stage.status().load is None
        with pytest.raises(MotionError):
            load_stage.tare()

    def test_measure_load_counts_is_the_mean_from_the_tare(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        wait_until(lambda: load_stage.status().load == 0.0, what="風袋引き")
        mcu.control("load", counts=-250)
        wait_until(lambda: load_stage.status().load == -2.5, what="-2.5 g")

        assert load_stage.measure_load_counts(0.25) == -250.0

    def test_measure_without_a_load_cell_is_a_motion_error(self, stage: McuMotionStage):
        with pytest.raises(MotionError, match="ロードセル"):
            stage.measure_load_counts(0.25)

    def test_measure_with_a_bad_sample_is_a_motion_error(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        wait_until(lambda: load_stage.status().load == 0.0, what="風袋引き")
        mcu.control("drop_blocks", count=1000)
        mcu.control("hx71x_error")

        with pytest.raises(MotionError):
            load_stage.measure_load_counts(1.0)


# 接触停止（load_stage は 100 counts/g、しきい値は既定の 50 g、戻るのは 15 g 未満。Z− で押し当たる）
_PRESS = 6000  # 60 g
_LIGHT = 1000  # 10 g


def _tared(stage: McuMotionStage) -> None:
    wait_until(lambda: stage.status().load == 0.0, what="風袋引き")


def _limits(mcu: WireMcu, since: int) -> list[tuple[int, int, int]]:
    return [
        (params["axis"], params["min_pos"], params["max_pos"])
        for name, params in mcu.accepted()[since:]
        if name == "velocity_stream_set_limits"
    ]


def _armed(mcu: WireMcu, since: int) -> list[int]:
    """張った（1）と外した（0）の trigger_analog_home の列."""
    return [
        int(params["monitor_ticks"] > 0)
        for name, params in mcu.accepted()[since:]
        if name == "trigger_analog_home"
    ]


def _press_with_the_stick(mcu: WireMcu, stage: McuMotionStage) -> None:
    """Z を原点に出し、スティックで Z− へ動かしている間に押させ、接触で止まるまで待つ."""
    _tared(stage)
    stage.home(["z"])

    def moving() -> bool:
        stage.set_velocity(Velocity(z=-20.0))
        return stage.status().position.z < -3.0

    wait_until(moving, what="Z が動き出すこと")
    mcu.control("load", counts=_PRESS)

    def stopped() -> bool:
        stage.set_velocity(Velocity(z=-20.0))
        return stage.status().contact and stage.status().state is MotionState.IDLE

    wait_until(stopped, what="接触で止まること")
    # MCU が止めたことには次の操作の最初で気づき、保持に入る（status は気づく前から接触中を返す）
    stage.stop()


class TestContactStop:
    def test_the_stick_stops_on_a_press_and_holds(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        _press_with_the_stick(mcu, load_stage)
        z = load_stage.status().position.z
        since = len(mcu.accepted())

        # X は原点が未だが、捨てるので拒まれない。Z− も捨て、Z+ だけ流す
        load_stage.set_velocity(Velocity(x=10.0, z=-5.0))

        def lifted() -> bool:
            load_stage.set_velocity(Velocity(x=10.0, z=5.0))
            return load_stage.status().position.z > z + 0.1

        wait_until(lifted, what="Z+ に動くこと")
        load_stage.stop()

        commands = mcu.accepted()[since:]
        sent = {
            (params["vx"], params["vy"], params["vz"])
            for name, params in commands
            if name == "velocity_stream_set_velocity"
        }
        assert sent == {(0, 0, 400)}
        # 保持の間は張らずに流す
        assert "trigger_analog_home" not in [name for name, _ in commands]
        assert "trsync_start" not in [name for name, _ in commands]
        assert load_stage.status().contact

    def test_holding_narrows_the_mcu_limits(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        since = len(mcu.accepted())

        _press_with_the_stick(mcu, load_stage)

        position = load_stage.status().position
        x, y, z = (round(value * 80) for value in (position.x, position.y, position.z))
        # X・Y は今の位置の ±1 step、Z は接触した位置より下（押す側）へ行けない。送るだけで応答を
        # 待たないので、MCU 役が受けたのを待つ
        wait_until(
            lambda: _limits(mcu, since)[-3:]
            == [(0, x - 1, x + 1), (1, y - 1, y + 1), (2, z, 0)],
            what="保持の可動域",
        )

    def test_releasing_restores_the_limits_and_arms_again(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        _press_with_the_stick(mcu, load_stage)
        mcu.control("load", counts=_LIGHT)
        wait_until(lambda: not load_stage.status().contact, what="接触中が消えること")
        since = len(mcu.accepted())

        load_stage.set_velocity(Velocity(z=-5.0))
        load_stage.stop()

        # 可動域を戻し（X・Y は原点出しの前の広い範囲、Z は原点出しの後の範囲）、張り直して流す
        assert _limits(mcu, since) == [
            (0, -3200, 3200),
            (1, -3200, 3200),
            (2, -800, 0),
        ]
        wait_until(lambda: _armed(mcu, since) == [1, 0], what="張って、止めたら外す")

    def test_a_load_while_idle_does_not_stop_or_hold(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        _tared(load_stage)
        load_stage.home(["z"])

        mcu.control("load", counts=_PRESS)
        wait_until(lambda: load_stage.status().load == 60.0, what="60 g")

        assert not load_stage.status().contact

    def test_releasing_the_stick_stops_and_disarms(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        _tared(load_stage)
        load_stage.home(["z"])
        since = len(mcu.accepted())

        def moving() -> bool:
            load_stage.set_velocity(Velocity(z=-20.0))
            return load_stage.status().position.z < -1.0

        wait_until(moving, what="Z が動き出すこと")
        # スティックを離すと全軸 0 を送り続ける。流すのをやめて接触停止を外すので、押されても止めない
        load_stage.set_velocity(Velocity())
        assert load_stage.status().state is MotionState.IDLE
        mcu.control("load", counts=_PRESS)
        wait_until(lambda: load_stage.status().load == 60.0, what="60 g")
        load_stage.set_velocity(Velocity())

        assert not load_stage.status().contact
        assert _armed(mcu, since) == [1, 0]
        assert _names(mcu, since).count("velocity_stream_start") == 1

    def test_a_move_into_the_load_raises_contact_stopped(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        _tared(load_stage)
        load_stage.home(["z"])
        mcu.control("load", counts=_PRESS)
        wait_until(lambda: load_stage.status().load == 60.0, what="60 g")

        # 遅く動かす。速いと、負荷の高いときに模擬 MCU の tick が空き、次のサンプルの前に着いてしまう
        with pytest.raises(ContactStopped):
            load_stage.move_to(z=-5.0, speed=1.0)

        assert load_stage.status().contact

    def test_holding_at_the_end_keeps_one_step_of_limits(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        # Z は原点（可動域の上端 0）にいて、離す向き（Z+）にはもう動けない
        _tared(load_stage)
        load_stage.home(["z"])
        mcu.control("load", counts=_PRESS)
        wait_until(lambda: load_stage.status().load == 60.0, what="60 g")
        since = len(mcu.accepted())

        def held() -> bool:
            load_stage.set_velocity(Velocity(z=5.0))
            return load_stage.status().contact

        wait_until(held, what="接触で止まること")
        load_stage.stop()

        wait_until(
            lambda: _limits(mcu, since)[-1:] == [(2, -1, 0)], what="保持の可動域"
        )

    def test_holding_refuses_homing_tare_and_pressing_moves(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        _press_with_the_stick(mcu, load_stage)
        z = load_stage.status().position.z

        with pytest.raises(ValueError, match="接触中は原点出し"):
            load_stage.home(["z"])
        with pytest.raises(ValueError, match="接触中は風袋引き"):
            load_stage.tare()
        with pytest.raises(ValueError, match="Z\\+"):
            load_stage.move_to(z=z - 1.0)

        load_stage.move_to(z=z + 1.0)
        assert load_stage.status().position.z == pytest.approx(z + 1.0, abs=_STEP)

    def test_homing_into_a_press_leaves_the_axis_unhomed(
        self, mcu: WireMcu, load_stage: McuMotionStage
    ):
        _tared(load_stage)
        load_stage.home(["z"])
        mcu.control("load", counts=_PRESS)
        wait_until(lambda: load_stage.status().load == 60.0, what="60 g")

        with pytest.raises(ContactStopped, match="X"):
            load_stage.home(["x"])

        status = load_stage.status()
        assert status.homed == frozenset({"z"})
        assert status.contact

    def test_relax_drops_the_hold(self, mcu: WireMcu, load_stage: McuMotionStage):
        _press_with_the_stick(mcu, load_stage)

        load_stage.relax()

        assert not load_stage.status().contact

    def test_without_a_load_cell_nothing_is_armed(
        self, mcu: WireMcu, stage: McuMotionStage
    ):
        stage.home(["z"])
        since = len(mcu.accepted())

        stage.set_velocity(Velocity(z=-5.0))
        stage.stop()

        names = _names(mcu, since)
        assert "trsync_start" not in names
        assert "trigger_analog_home" not in names


class TestLoadCellFault:
    def test_refuses_to_move_before_the_tare(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        mcu.control("hx71x_stall")
        stage = McuMotionStage(
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
        try:
            with pytest.raises(MotionError, match="ロードセルが読めない"):
                stage.home(["z"])
        finally:
            stage.close()

    @pytest.mark.parametrize(
        ("op", "params", "name"),
        [
            ("hx71x_stall", {}, "MONITOR"),
            ("load", {"counts": -(1 << 23)}, "RAW_RANGE"),
        ],
        ids=["no-samples", "stuck"],
    )
    def test_a_fault_while_moving_is_an_error_not_a_hold(
        self,
        mcu: WireMcu,
        load_stage: McuMotionStage,
        op: str,
        params: dict,
        name: str,
    ):
        _tared(load_stage)
        load_stage.home(["z"])
        load_stage.set_velocity(Velocity(z=-5.0))
        mcu.control(op, **params)

        def failed() -> bool:
            try:
                load_stage.set_velocity(Velocity(z=-5.0))
            except MotionError as error:
                assert name in str(error)
                return True
            return False

        wait_until(failed, what=f"{name} の異常")
        assert not load_stage.status().contact
