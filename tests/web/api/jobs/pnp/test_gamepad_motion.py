"""gamepad_motion ジョブの仕様テスト（新ファームの機体、FakeMotionStage と FakeGamepad）.

- 中止されるまで、コントローラと画面の仮想スティック（WS command の ``stick``）で XYZ を動かす
  - 倒し切り（1）が各軸の最高速度。向きは「右 = X+、上 = Y−、上 = Z+」
  - 仮想スティックは、届いてから ``VIRTUAL_TIMEOUT`` を過ぎると 0 になる（送信が途切れたら止まる）
  - 一度に届いた stick は最後の 1 件だけを使う
  - マシン操作パネルの命令と一緒に届いた stick は捨てる（原点出しや移動の間に溜まった古い入力で動かない）
- 同じジョブの中で、マシン操作パネルの命令（原点出し・ジョグ・移動・脱力・風袋引き）を届いた順に実行する
  - Klipper の機体だけの命令と、ロードセルの無い機体の風袋引きと、形の誤った stick と、知らない command は
    ログに出して続ける
- コントローラのスティックには machine.toml の [pnp.gamepad] のデッドゾーンを掛ける
- コントローラが外れても、ジョブを落とさず仮想スティックで続ける
- [pnp.servo] が無くても動く（ピンセットは使わない）
- 中止で終わり、ステージを止める。コントローラは閉じる
- ステージが shutdown したら失敗で終わる
- 実機での確認は受け入れ試験（docs/acceptance.md）
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Collection, Iterator
from typing import Any, override

import pytest
import tomlkit

from pcbasm.hal import Axis, GamepadState, Velocity
from pcbasm.hal.fake import FakeAudioPlayer, FakeGamepad, FakeMotionStage
from pcbasm.machine.devices import Devices
from pcbasm.pnp.gamepad_control import VIRTUAL_TIMEOUT
from tests.support.polling import wait_until
from tests.support.web import fake_motion_stage, job_manager, use_motion_machine
from web.api.backend import Backend, open_backend
from web.api.env import Env
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobRecord, JobStatus


@pytest.fixture
def stage() -> FakeMotionStage:
    return fake_motion_stage(homed=True)


@pytest.fixture
def pad() -> FakeGamepad:
    return FakeGamepad()


@pytest.fixture
def backend(
    webui_settings: Env,
    audio_player: FakeAudioPlayer,
    stage: FakeMotionStage,
    pad: FakeGamepad,
) -> Iterator[Backend]:
    path = use_motion_machine(webui_settings.config_dir)
    devices = Devices(
        path, webui_settings.journal_dir, audio=audio_player, motion=stage, gamepad=pad
    )
    backend = open_backend(webui_settings, devices=devices)
    yield backend
    backend.close()


@pytest.fixture
def manager(backend: Backend) -> Iterator[JobManager]:
    manager = job_manager(backend, backend.catalog)
    yield manager
    manager.shutdown()


@pytest.fixture
def record(manager: JobManager) -> JobRecord:
    """走り出したコントローラ操作のジョブ."""
    record = manager.start("gamepad_motion", {})
    wait_until(lambda: record.status == JobStatus.RUNNING)
    return record


def _send(manager: JobManager, record: JobRecord, command: dict[str, Any]) -> None:
    manager.submit_command(command, expected_job_id=record.id)


def _stick(x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict[str, Any]:
    return {"type": "stick", "x": x, "y": y, "z": z}


def _last(stage: FakeMotionStage) -> Velocity | None:
    velocities = stage.velocities
    return velocities[-1][1] if velocities else None


def _log(record: JobRecord) -> str:
    return "\n".join(record.log_lines)


class TestVirtualStick:
    @pytest.mark.parametrize(
        ("stick", "expected"),
        [
            (_stick(x=1.0), lambda m: Velocity(x=m.x)),
            (_stick(y=1.0), lambda m: Velocity(y=-m.y)),
            (_stick(z=1.0), lambda m: Velocity(z=m.z)),
        ],
        ids=["right-is-x-plus", "up-is-y-minus", "up-is-z-plus"],
    )
    def test_full_tilt_is_the_max_velocity(
        self,
        manager: JobManager,
        record: JobRecord,
        stage: FakeMotionStage,
        stick: dict[str, Any],
        expected: Any,
    ):
        _send(manager, record, stick)

        wait_until(lambda: _last(stage) == expected(stage.max_velocity))

    def test_stops_when_the_input_stops_arriving(
        self, manager: JobManager, record: JobRecord, stage: FakeMotionStage
    ):
        _send(manager, record, _stick(x=0.5))
        wait_until(lambda: (_last(stage) or Velocity()).x > 0.0)

        wait_until(lambda: _last(stage) == Velocity())

        moving = [t for t, v in stage.velocities if v != Velocity()]
        stopped = next(
            t for t, v in stage.velocities if t > moving[0] and v == Velocity()
        )
        # 届いたあと 1 周期（20 ms）以内に動き出し、届いてから VIRTUAL_TIMEOUT を過ぎて止まる
        assert stopped - moving[0] >= VIRTUAL_TIMEOUT - 0.05
        assert record.status == JobStatus.RUNNING

    def test_settles_on_the_last_stick_of_a_burst(
        self, manager: JobManager, record: JobRecord, stage: FakeMotionStage
    ):
        for x in (1.0, 0.5, -1.0):
            _send(manager, record, _stick(x=x))

        wait_until(lambda: _last(stage) == Velocity(x=-stage.max_velocity.x))

    def test_malformed_stick_is_logged_and_ignored(
        self, manager: JobManager, record: JobRecord, stage: FakeMotionStage
    ):
        _send(manager, record, _stick(x=2.0))

        wait_until(lambda: "仮想スティックの入力を読めません" in _log(record))
        assert all(v == Velocity() for _, v in stage.velocities)
        assert record.status == JobStatus.RUNNING


class TestController:
    def test_left_stick_drives_xy(
        self,
        record: JobRecord,
        stage: FakeMotionStage,
        pad: FakeGamepad,
    ):
        pad.set_state(GamepadState(left_x=1.0))

        wait_until(lambda: _last(stage) == Velocity(x=stage.max_velocity.x))

    def test_applies_the_deadzone_from_machine_toml(
        self,
        backend: Backend,
        manager: JobManager,
        stage: FakeMotionStage,
        pad: FakeGamepad,
    ):
        path = backend.machine_config.path
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        document["pnp"]["gamepad"] = {"deadzone_xy": 0.5}  # type: ignore[index]
        path.write_text(tomlkit.dumps(document), encoding="utf-8")
        pad.set_state(GamepadState(left_x=0.75))

        record = manager.start("gamepad_motion", {})

        # 0.5〜1 を 0〜1 へスケールし直すので、0.75 は半分の速さ（既定の 0.15 なら 0.7 ほど）
        wait_until(lambda: _last(stage) is not None)
        velocity = _last(stage)
        assert velocity is not None
        assert (velocity.x, velocity.y, velocity.z) == (
            pytest.approx(0.5 * stage.max_velocity.x),
            0.0,
            0.0,
        )
        assert record.status == JobStatus.RUNNING

    def test_keeps_running_on_the_virtual_stick_after_the_controller_is_lost(
        self,
        manager: JobManager,
        record: JobRecord,
        stage: FakeMotionStage,
        pad: FakeGamepad,
    ):
        pad.disconnect()

        _send(manager, record, _stick(x=1.0))

        wait_until(lambda: _last(stage) == Velocity(x=stage.max_velocity.x))
        assert record.status == JobStatus.RUNNING


class TestPanelCommands:
    def test_runs_panel_commands_in_order(
        self, manager: JobManager, record: JobRecord, stage: FakeMotionStage
    ):
        _send(manager, record, {"type": "move", "x": 10.0})
        _send(manager, record, {"type": "jog", "axis": "x", "distance": 2.5})

        wait_until(lambda: stage.status().position.x == 12.5)
        assert record.status == JobStatus.RUNNING

    def test_relax_is_not_undone_by_the_stick(
        self,
        manager: JobManager,
        record: JobRecord,
        stage: FakeMotionStage,
        caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.WARNING)
        _send(manager, record, {"type": "relax"})
        wait_until(lambda: "relax" in stage.commands)
        sent = len(stage.velocities)

        _send(manager, record, _stick(x=1.0))

        wait_until(lambda: "原点出しをしていない軸" in caplog.text)
        assert stage.status().homed == frozenset()
        assert all(v == Velocity() for _, v in stage.velocities[sent:])

    @pytest.mark.parametrize("kind", ["focus_z", "move_to_cap"])
    def test_klipper_only_command_is_logged(
        self, manager: JobManager, record: JobRecord, kind: str
    ):
        _send(manager, record, {"type": kind})

        wait_until(lambda: "この機体では使えません" in _log(record))
        assert record.status == JobStatus.RUNNING

    def test_tare_without_a_load_cell_is_logged_and_the_job_goes_on(
        self, manager: JobManager, record: JobRecord
    ):
        _send(manager, record, {"type": "tare"})

        wait_until(lambda: "ロードセル" in _log(record))
        assert record.status == JobStatus.RUNNING

    def test_unknown_command_is_logged(self, manager: JobManager, record: JobRecord):
        _send(manager, record, {"type": "bogus"})

        wait_until(lambda: "bogus" in _log(record))
        assert record.status == JobStatus.RUNNING


class TestTare:
    @pytest.fixture
    def stage(self) -> FakeMotionStage:
        return fake_motion_stage(homed=True, load_cell=True)

    def test_zeroes_the_load(
        self, manager: JobManager, record: JobRecord, stage: FakeMotionStage
    ):
        stage.set_load(3.0)

        _send(manager, record, {"type": "tare"})

        wait_until(lambda: stage.status().load == 0.0, what="風袋引き")
        assert record.status == JobStatus.RUNNING


class _HomeWaitsForGate(FakeMotionStage):
    """原点出しが gate を待つ FakeMotionStage（原点出しの間に stick を溜めるため）."""

    def __init__(self, base: FakeMotionStage, gate: threading.Event) -> None:
        super().__init__(base.limits, base.max_velocity, homed=True)
        self._gate = gate

    @override
    def home(self, axes: Collection[Axis] = ()) -> None:
        self._gate.wait(timeout=10.0)
        super().home(axes)


class TestBlockingCommand:
    @pytest.fixture
    def gate(self) -> threading.Event:
        return threading.Event()

    @pytest.fixture
    def stage(self, gate: threading.Event) -> FakeMotionStage:
        return _HomeWaitsForGate(fake_motion_stage(), gate)

    def test_drops_sticks_queued_behind_homing(
        self,
        manager: JobManager,
        record: JobRecord,
        stage: FakeMotionStage,
        pad: FakeGamepad,
        gate: threading.Event,
    ):
        _send(manager, record, {"type": "home"})
        # 原点出しの間に、仮想スティックの入力が溜まる（送り手はその後いなくなる）
        for _ in range(3):
            _send(manager, record, _stick(x=1.0))
        pad.set_state(GamepadState(left_y=1.0))

        gate.set()

        # 古い stick が効いていれば、コントローラより先に X が動く
        wait_until(lambda: _last(stage) == Velocity(y=-stage.max_velocity.y))
        assert "home x" in stage.commands
        assert all(v.x == 0.0 for _, v in stage.velocities)


class TestLifecycle:
    def test_runs_without_the_tweezer(self, record: JobRecord):
        """検証用の machine.toml には [pnp.servo] が無い."""
        wait_until(lambda: "ピンセット" in _log(record))
        assert record.status == JobStatus.RUNNING

    def test_abort_stops_the_stage_and_closes_the_controller(
        self,
        manager: JobManager,
        record: JobRecord,
        stage: FakeMotionStage,
        pad: FakeGamepad,
    ):
        _send(manager, record, _stick(x=1.0))
        wait_until(lambda: _last(stage) == Velocity(x=stage.max_velocity.x))

        assert manager.request_abort()
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.ABORTED
        assert "stop" in stage.commands
        assert stage.status().velocity == Velocity()
        assert pad.closed

    def test_stage_shutdown_fails_the_job(
        self, manager: JobManager, record: JobRecord, stage: FakeMotionStage
    ):
        stage.shutdown("Command request")

        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.FAILED
        assert record.error is not None and "shutdown" in record.error

    def test_holds_the_machine_lock_while_running(
        self, manager: JobManager, backend: Backend, record: JobRecord
    ):
        assert backend.lock.owner == "job:gamepad_motion"

        manager.request_abort()
        wait_until(lambda: record.status.terminal)
