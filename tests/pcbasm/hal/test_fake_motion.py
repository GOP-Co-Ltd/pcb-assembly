"""`pcbasm.hal.fake.FakeMotionStage` の仕様テスト."""

import pytest

from pcbasm.geometry import Point3d
from pcbasm.hal import (
    ContactSettings,
    ContactStopped,
    Limits,
    MotionError,
    MotionStage,
    MotionState,
    ScalarLimits,
    Velocity,
)
from pcbasm.hal.fake import FakeMotionStage

LIMITS = Limits(
    x=ScalarLimits(0.0, 100.0),
    y=ScalarLimits(0.0, 80.0),
    z=ScalarLimits(-40.0, 0.0),
    v=ScalarLimits(0.0, 50.0),
)
MAX_VELOCITY = Velocity(x=50.0, y=50.0, z=10.0)


class _Clock:
    """テストが手で進める時計."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _stage(clock: _Clock, *, homed: bool = True, ttl: float = 0.2) -> FakeMotionStage:
    return FakeMotionStage(LIMITS, MAX_VELOCITY, ttl=ttl, homed=homed, clock=clock)


class TestFakeMotionStageBasics:
    """初期状態と、ABC としての値."""

    def test_is_a_motion_stage(self, clock: _Clock):
        assert isinstance(_stage(clock), MotionStage)

    def test_exposes_limits_and_max_velocity(self, clock: _Clock):
        stage = _stage(clock)

        assert stage.limits == LIMITS
        assert stage.max_velocity == MAX_VELOCITY

    def test_starts_idle_at_origin_clamped_into_limits(self, clock: _Clock):
        limits = Limits(
            x=ScalarLimits(10.0, 100.0),
            y=ScalarLimits(-20.0, -5.0),
            z=ScalarLimits(-40.0, 0.0),
            v=ScalarLimits(0.0, 50.0),
        )
        stage = FakeMotionStage(limits, MAX_VELOCITY, clock=clock)

        status = stage.status()

        assert status.position == Point3d(10.0, -5.0, 0.0)
        assert status.velocity == Velocity()
        assert status.state is MotionState.IDLE
        assert status.homed == frozenset()
        assert status.shutdown_reason is None

    def test_homed_true_marks_all_axes_homed(self, clock: _Clock):
        assert _stage(clock, homed=True).status().homed == {"x", "y", "z"}


class TestFakeMotionStageVelocity:
    """速度指令の積分・期限・クランプ."""

    def test_integrates_velocity_over_clock(self, clock: _Clock):
        stage = _stage(clock)
        stage.move_to(10.0, 10.0, -20.0)

        stage.set_velocity(Velocity(x=10.0, y=-5.0, z=2.0))
        clock.now += 0.1

        status = stage.status()
        assert status.position.x == pytest.approx(11.0)
        assert status.position.y == pytest.approx(9.5)
        assert status.position.z == pytest.approx(-19.8)
        assert status.velocity == Velocity(x=10.0, y=-5.0, z=2.0)
        assert status.state is MotionState.MOVING

    def test_records_each_send_with_time(self, clock: _Clock):
        stage = _stage(clock)

        stage.set_velocity(Velocity(x=1.0))
        clock.now += 0.05
        stage.set_velocity(Velocity(y=2.0))

        assert stage.velocities == [(0.0, Velocity(x=1.0)), (0.05, Velocity(y=2.0))]

    def test_velocity_expires_after_ttl(self, clock: _Clock):
        stage = _stage(clock, ttl=0.2)
        stage.move_to(10.0, 10.0, -20.0)

        stage.set_velocity(Velocity(x=10.0))
        clock.now += 1.0

        status = stage.status()
        assert status.position.x == pytest.approx(12.0)
        assert status.velocity == Velocity()

    def test_new_send_extends_ttl(self, clock: _Clock):
        stage = _stage(clock, ttl=0.2)
        stage.move_to(10.0, 10.0, -20.0)

        stage.set_velocity(Velocity(x=10.0))
        clock.now += 0.15
        stage.set_velocity(Velocity(x=10.0))
        clock.now += 0.15

        assert stage.status().position.x == pytest.approx(13.0)

    def test_clamps_position_into_limits(self, clock: _Clock):
        stage = _stage(clock, ttl=10.0)
        stage.move_to(95.0, 5.0, -1.0)

        stage.set_velocity(Velocity(x=10.0, y=-10.0, z=10.0))
        clock.now += 1.0

        assert stage.status().position == Point3d(100.0, 0.0, 0.0)

    def test_all_zero_while_idle_does_nothing(self, clock: _Clock):
        stage = _stage(clock)

        stage.set_velocity(Velocity())

        assert stage.status().state is MotionState.IDLE
        assert stage.velocities == []

    def test_all_zero_while_moving_is_sent_and_stops(self, clock: _Clock):
        stage = _stage(clock)
        stage.set_velocity(Velocity(x=1.0))

        stage.set_velocity(Velocity())

        assert stage.velocities[-1] == (0.0, Velocity())
        assert stage.status().state is MotionState.IDLE

    @pytest.mark.parametrize(
        "velocity", [Velocity(x=1.0), Velocity(y=-1.0), Velocity(z=0.5)]
    )
    def test_rejects_nonzero_on_unhomed_axis(self, clock: _Clock, velocity: Velocity):
        stage = _stage(clock, homed=False)

        with pytest.raises(ValueError, match="原点"):
            stage.set_velocity(velocity)
        assert stage.velocities == []

    def test_accepts_velocity_on_homed_axes_only(self, clock: _Clock):
        stage = _stage(clock, homed=False)
        stage.home(["z"])

        stage.set_velocity(Velocity(z=-1.0))

        assert stage.status().state is MotionState.MOVING

    def test_stop_halts_and_returns_to_idle(self, clock: _Clock):
        stage = _stage(clock)
        stage.move_to(10.0, 10.0, -20.0)
        stage.set_velocity(Velocity(x=10.0))
        clock.now += 0.1

        stage.stop()
        clock.now += 0.1

        status = stage.status()
        assert status.position.x == pytest.approx(11.0)
        assert status.velocity == Velocity()
        assert status.state is MotionState.IDLE
        assert stage.commands[-1] == "stop"


class TestFakeMotionStageHome:
    """原点出し."""

    def test_home_all_in_z_x_y_order(self, clock: _Clock):
        stage = _stage(clock, homed=False)

        stage.home()

        assert stage.commands == ["home z", "home x", "home y"]
        assert stage.status().homed == {"x", "y", "z"}

    def test_home_xy_homes_unhomed_z_first(self, clock: _Clock):
        stage = _stage(clock, homed=False)

        stage.home(["x", "y"])

        assert stage.commands == ["home z", "home x", "home y"]

    def test_home_puts_axis_at_origin_clamped_into_limits(self, clock: _Clock):
        stage = _stage(clock)
        stage.move_to(50.0, 50.0, -20.0)

        stage.home(["x"])

        assert stage.status().position == Point3d(0.0, 50.0, -20.0)

    def test_home_stops_the_stream_and_velocity_can_restart(self, clock: _Clock):
        stage = _stage(clock)
        stage.set_velocity(Velocity(x=1.0))

        stage.home(["x"])

        assert stage.status().state is MotionState.IDLE
        stage.set_velocity(Velocity(x=1.0))
        assert stage.status().state is MotionState.MOVING


class TestFakeMotionStageMoveTo:
    """座標指定の移動."""

    def test_moves_given_axes_only(self, clock: _Clock):
        stage = _stage(clock)

        stage.move_to(x=30.0, z=-5.0, speed=20.0)

        assert stage.status().position == Point3d(30.0, 0.0, -5.0)
        assert stage.commands == ["move_to"]

    def test_stops_stream_and_ends_idle(self, clock: _Clock):
        stage = _stage(clock)
        stage.set_velocity(Velocity(x=1.0))

        stage.move_to(x=30.0)

        status = stage.status()
        assert status.state is MotionState.IDLE
        assert status.velocity == Velocity()

    @pytest.mark.parametrize(
        "kwargs",
        [{"x": 101.0}, {"y": -0.1}, {"z": 1.0}, {"x": 10.0, "speed": 51.0}],
    )
    def test_rejects_outside_limits(self, clock: _Clock, kwargs: dict[str, float]):
        stage = _stage(clock)

        with pytest.raises(ValueError, match="可動域"):
            stage.move_to(**kwargs)
        assert stage.status().position == Point3d(0.0, 0.0, 0.0)

    def test_rejects_unhomed_axis(self, clock: _Clock):
        stage = _stage(clock, homed=False)
        stage.home(["x"])

        with pytest.raises(ValueError, match="原点"):
            stage.move_to(x=10.0, y=10.0)


class TestFakeMotionStageRelaxRestart:
    """脱力と再起動で原点が未に戻る."""

    def test_relax_stops_and_clears_homed(self, clock: _Clock):
        stage = _stage(clock)
        stage.set_velocity(Velocity(x=1.0))

        stage.relax()

        status = stage.status()
        assert status.state is MotionState.IDLE
        assert status.homed == frozenset()
        assert stage.commands[-1] == "relax"

    def test_restart_clears_shutdown_and_homed(self, clock: _Clock):
        stage = _stage(clock)
        stage.emergency_stop()

        stage.restart()

        status = stage.status()
        assert status.state is MotionState.IDLE
        assert status.homed == frozenset()
        assert status.shutdown_reason is None


class TestFakeMotionStageShutdown:
    """緊急停止と MCU の shutdown."""

    def test_emergency_stop_shuts_down(self, clock: _Clock):
        stage = _stage(clock)
        stage.set_velocity(Velocity(x=10.0))

        stage.emergency_stop()
        clock.now += 1.0

        status = stage.status()
        assert status.state is MotionState.SHUTDOWN
        assert status.velocity == Velocity()
        assert status.position == Point3d(0.0, 0.0, 0.0)
        assert status.shutdown_reason is not None
        assert stage.commands[-1] == "emergency_stop"

    def test_shutdown_reports_reason(self, clock: _Clock):
        stage = _stage(clock)

        stage.shutdown("Timer too close")

        status = stage.status()
        assert status.state is MotionState.SHUTDOWN
        assert status.shutdown_reason == "Timer too close"

    @pytest.mark.parametrize(
        "operation",
        [
            lambda s: s.set_velocity(Velocity(x=1.0)),
            lambda s: s.set_velocity(Velocity()),
            lambda s: s.home(),
            lambda s: s.move_to(x=1.0),
            lambda s: s.stop(),
            lambda s: s.relax(),
        ],
        ids=["set_velocity", "set_velocity_zero", "home", "move_to", "stop", "relax"],
    )
    def test_operations_after_shutdown_raise(self, clock: _Clock, operation):
        stage = _stage(clock)
        stage.shutdown("Timer too close")

        with pytest.raises(MotionError, match="Timer too close"):
            operation(stage)


class TestFakeMotionStageDisconnect:
    """USB が外れたとき."""

    @pytest.mark.parametrize(
        "operation",
        [
            lambda s: s.status(),
            lambda s: s.set_velocity(Velocity(x=1.0)),
            lambda s: s.home(),
            lambda s: s.move_to(x=1.0),
            lambda s: s.stop(),
            lambda s: s.relax(),
            lambda s: s.restart(),
            lambda s: s.emergency_stop(),
        ],
        ids=[
            "status",
            "set_velocity",
            "home",
            "move_to",
            "stop",
            "relax",
            "restart",
            "emergency_stop",
        ],
    )
    def test_operations_after_disconnect_raise(self, clock: _Clock, operation):
        stage = _stage(clock)

        stage.disconnect()

        with pytest.raises(MotionError):
            operation(stage)

    def test_close_is_recorded(self, clock: _Clock):
        stage = _stage(clock)

        stage.close()

        assert stage.closed


class TestFakeMotionStageLoad:
    """ロードセルの荷重は外から与え、:meth:`tare` がその時点の値を 0 にする."""

    def _load_stage(self, clock: _Clock) -> FakeMotionStage:
        return FakeMotionStage(LIMITS, MAX_VELOCITY, clock=clock, load_cell=True)

    def test_without_a_load_cell_there_is_no_load(self, clock: _Clock):
        assert _stage(clock).status().load is None

    def test_starts_at_zero_and_reports_the_given_load(self, clock: _Clock):
        stage = self._load_stage(clock)
        assert stage.status().load == 0.0

        stage.set_load(12.5)

        assert stage.status().load == 12.5

    def test_tare_zeroes_the_current_load(self, clock: _Clock):
        stage = self._load_stage(clock)
        stage.set_load(3.0)

        stage.tare()
        stage.set_load(5.0)

        assert stage.status().load == 2.0
        assert stage.commands == ["tare"]

    def test_tare_without_a_load_cell_is_rejected(self, clock: _Clock):
        with pytest.raises(ValueError):
            _stage(clock).tare()

    def test_shutdown_hides_the_load_and_refuses_tare(self, clock: _Clock):
        stage = self._load_stage(clock)
        stage.shutdown("Command request")

        assert stage.status().load is None
        with pytest.raises(MotionError):
            stage.tare()
        with pytest.raises(MotionError):
            stage.measure_load_counts(1.0)

    def test_measure_load_counts_is_the_load_from_the_tare_in_counts(
        self, clock: _Clock
    ):
        stage = FakeMotionStage(
            LIMITS, MAX_VELOCITY, clock=clock, load_cell=True, counts_per_gram=2000.0
        )
        stage.set_load(3.0)
        stage.tare()

        stage.set_load(-2.0)

        assert stage.measure_load_counts(1.0) == -10000.0

    def test_measure_without_a_load_cell_is_a_motion_error(self, clock: _Clock):
        with pytest.raises(MotionError):
            _stage(clock).measure_load_counts(1.0)

    def test_a_load_error_hides_the_load_and_refuses_measuring(self, clock: _Clock):
        stage = FakeMotionStage(LIMITS, MAX_VELOCITY, clock=clock, load_cell=True)
        stage.set_load(3.0)

        stage.set_load_error("READ_TOO_LONG")
        status = stage.status()
        assert (status.load, status.load_error) == (None, "READ_TOO_LONG")
        with pytest.raises(MotionError, match="READ_TOO_LONG"):
            stage.measure_load_counts(1.0)

        stage.set_load_error(None)
        assert stage.status().load == 3.0

    def test_suspend_auto_tare_is_recorded_only_with_a_load_cell(self, clock: _Clock):
        stage = FakeMotionStage(LIMITS, MAX_VELOCITY, clock=clock, load_cell=True)
        without = _stage(clock)

        for each in (stage, without):
            each.suspend_auto_tare(True)
            each.suspend_auto_tare(False)

        assert stage.commands == ["suspend_auto_tare", "resume_auto_tare"]
        assert without.commands == []


class TestFakeMotionStageContact:
    """接触停止（風袋からの荷重が trigger_force 以上で止める）と接触保持（kurousagi と同じく Z− で押し当たる）."""

    def _contact_stage(self, clock: _Clock) -> FakeMotionStage:
        return FakeMotionStage(
            LIMITS,
            MAX_VELOCITY,
            homed=True,
            clock=clock,
            load_cell=True,
            contact=ContactSettings(trigger_force=50.0, release_force=15.0),
        )

    def _press(self, stage: FakeMotionStage, clock: _Clock) -> None:
        stage.set_velocity(Velocity(z=-5.0))
        clock.now += 0.1
        stage.set_load(60.0)

    def test_stops_and_holds_when_pressed_while_moving(self, clock: _Clock):
        stage = self._contact_stage(clock)

        self._press(stage, clock)

        status = stage.status()
        assert status.state is MotionState.IDLE
        assert status.contact
        assert status.position.z == pytest.approx(-0.5)

    def test_a_load_while_idle_does_not_hold(self, clock: _Clock):
        stage = self._contact_stage(clock)

        stage.set_load(60.0)

        assert not stage.status().contact

    def test_a_load_after_the_stick_returns_to_zero_does_not_hold(self, clock: _Clock):
        stage = self._contact_stage(clock)
        stage.set_velocity(Velocity(z=-5.0))
        clock.now += 0.1

        # スティックを離すと全軸 0 を送り続ける。止まっている間は押されても止めない
        stage.set_velocity(Velocity())
        stage.set_load(60.0)
        stage.set_velocity(Velocity())

        status = stage.status()
        assert status.state is MotionState.IDLE
        assert not status.contact

    def test_holding_drops_xy_and_the_pressing_direction(self, clock: _Clock):
        stage = self._contact_stage(clock)
        self._press(stage, clock)
        sent = len(stage.velocities)

        stage.set_velocity(Velocity(x=10.0, y=10.0, z=-5.0))
        stage.set_velocity(Velocity(x=10.0, z=5.0))

        # 捨てて全軸 0 になった指令は、止まっているので送らない
        assert [velocity for _, velocity in stage.velocities[sent:]] == [
            Velocity(z=5.0)
        ]

    def test_holding_refuses_home_tare_and_moves_toward_the_tweezers(
        self, clock: _Clock
    ):
        stage = self._contact_stage(clock)
        self._press(stage, clock)

        with pytest.raises(ValueError, match="接触中"):
            stage.home()
        with pytest.raises(ValueError, match="接触中"):
            stage.tare()
        with pytest.raises(ValueError, match="X・Y"):
            stage.move_to(x=1.0)
        with pytest.raises(ValueError, match="Z+"):
            stage.move_to(z=-1.0)
        stage.move_to(z=0.0)
        assert stage.status().position.z == 0.0

    def test_releases_on_the_next_operation_after_the_load_drops(self, clock: _Clock):
        stage = self._contact_stage(clock)
        self._press(stage, clock)

        stage.set_load(10.0)

        assert not stage.status().contact
        stage.set_velocity(Velocity(x=10.0))
        assert stage.velocities[-1][1] == Velocity(x=10.0)

    def test_moving_into_a_load_raises_contact_stopped(self, clock: _Clock):
        stage = self._contact_stage(clock)
        stage.set_load(60.0)

        with pytest.raises(ContactStopped):
            stage.move_to(z=-10.0)

        assert stage.status().contact
        assert stage.status().position.z == 0.0

    def test_relax_drops_the_hold(self, clock: _Clock):
        stage = self._contact_stage(clock)
        self._press(stage, clock)

        stage.relax()

        assert not stage.status().contact
