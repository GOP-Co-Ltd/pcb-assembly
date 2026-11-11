"""gamepad_control ジョブの仕様テスト（部品実装だけの機体、FakeGamepad と FakeServoBus）.

- 中止されるまで、コントローラの入力でピンセットを動かす（LT=開閉、LB / RB=回転）
- 中止で終わり、回転を止めてトルクは保持する（つまんだ部品を落とさない）。コントローラは閉じる
- コントローラが外れたら失敗で終わる（そのときも回転を止める）
- 実機での確認は受け入れ試験（docs/acceptance.md の A27）
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import tomlkit

from pcbasm.hal import GamepadState, ServoMode
from pcbasm.hal.fake import FakeAudioPlayer, FakeGamepad, FakeServoBus, ServoCommand
from pcbasm.machine.devices import Devices
from tests.support.machine_toml import pnp_only
from tests.support.polling import wait_until
from tests.support.web import job_manager
from web.api.backend import Backend, open_backend
from web.api.env import Env
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobStatus
from web.api.machine_config import MachineConfigFile

# 閉じ切り 180° = raw 2048、開き切り 225° = raw 2560
_SERVO = {
    "port": "/dev/ttyUSB0",
    "rotation_id": 1,
    "open_close_id": 2,
    "closed_angle": 180.0,
    "open_angle": 225.0,
}
# 既定の 45°/s: 止めると 27 step だけ残す
STOPPING = 27


@pytest.fixture
def bus() -> FakeServoBus:
    return FakeServoBus(
        {1: ServoMode.STEP, 2: ServoMode.POSITION},
        {2: 2048},
        angle_limits={1: (0, 0), 2: (2048, 2560)},
    )


@pytest.fixture
def pad() -> FakeGamepad:
    return FakeGamepad()


@pytest.fixture
def backend(
    webui_settings: Env, bus: FakeServoBus, pad: FakeGamepad
) -> Iterator[Backend]:
    path = MachineConfigFile(webui_settings.config_dir).path
    document = tomlkit.parse(pnp_only(path.read_text()))
    table = tomlkit.table()
    table.update(_SERVO)
    document["pnp"]["servo"] = table  # type: ignore[index]
    path.write_text(tomlkit.dumps(document))
    devices = Devices(
        path,
        webui_settings.journal_dir,
        audio=FakeAudioPlayer(),
        servo_bus=bus,
        gamepad=pad,
    )
    backend = open_backend(webui_settings, devices=devices)
    yield backend
    backend.close()


@pytest.fixture
def manager(backend: Backend) -> Iterator[JobManager]:
    manager = job_manager(backend, backend.catalog)
    yield manager
    manager.shutdown()


class TestGamepadControl:
    def test_follows_the_controller_until_aborted_and_holds(
        self, manager: JobManager, bus: FakeServoBus, pad: FakeGamepad
    ):
        record = manager.start("gamepad_control", {})

        pad.set_state(GamepadState(left_trigger=1.0))
        wait_until(lambda: ServoCommand(2, "goto", 2560) in bus.commands)
        pad.set_state(GamepadState(left_trigger=1.0, right_bumper=True))
        wait_until(lambda: bus.remaining_steps(1) > STOPPING)
        assert record.status == JobStatus.RUNNING

        assert manager.request_abort()
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.ABORTED
        assert bus.remaining_steps(1) == STOPPING  # 回転は止める
        assert ServoCommand(254, "torque", 0) not in bus.commands  # 脱力しない
        assert pad.closed

    def test_disconnected_controller_fails_the_job_and_stops_rotating(
        self, manager: JobManager, bus: FakeServoBus, pad: FakeGamepad
    ):
        pad.set_state(GamepadState(left_bumper=True))
        record = manager.start("gamepad_control", {})
        wait_until(lambda: bus.remaining_steps(1) < -STOPPING)

        pad.disconnect()
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.FAILED
        assert record.error is not None and "切断" in record.error
        assert bus.remaining_steps(1) == -STOPPING
        assert pad.closed

    def test_holds_the_machine_lock_while_running(
        self, manager: JobManager, backend: Backend
    ):
        record = manager.start("gamepad_control", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        assert backend.lock.owner == "job:gamepad_control"

        manager.request_abort()
        wait_until(lambda: record.status.terminal)
