"""servo_setup ジョブの仕様テスト（部品実装だけの機体、サーボは FakeServoBus）.

- 始める前: ピンセットを中ほどにしてもらってから振る
- 役割の判別: 振ってから尋ね、同じ役割を 2 回選ぶと尋ね直す。「もう一度動かす」で振り直す
- 開閉の範囲: トルクを切って人が手で回した位置を読み、使えない範囲なら読み直す。手を離してもらってから
  確認の開閉をし、止まりきらなければトルクを切って読み直す。確認で「やり直す」なら読み直す
- 結果: Apply の値は [pnp.servo] の 4 値で、設定ページと同じ書き込みで machine.toml に入る
- 実機のサーボでの確認は受け入れ試験（docs/acceptance.md の A23）
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import tomlkit

from pcbasm.hal import ServoError, ServoMode
from pcbasm.hal.fake import FakeAudioPlayer, FakeServoBus, ServoCommand
from pcbasm.machine.devices import Devices
from pcbasm.pnp.servo_setup import ServoRole
from tests.support.machine_toml import pnp_only
from tests.support.polling import wait_until
from tests.support.web import job_manager
from web.api.backend import Backend, open_backend
from web.api.env import Env
from web.api.jobs.pnp.servo_setup import AGAIN
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobRecord, JobStatus, PromptSpec
from web.api.machine_config import MachineConfigFile

ROTATION = ServoRole.ROTATION.label
OPEN_CLOSE = ServoRole.OPEN_CLOSE.label
PARAMS = {"max_id": 5}


def _with_servo(text: str, *, servo: bool = True) -> str:
    document = tomlkit.parse(pnp_only(text))
    if servo:
        table = tomlkit.table()
        table["port"] = "/dev/ttyUSB0"
        document["pnp"]["servo"] = table  # type: ignore[index]
    return tomlkit.dumps(document)


@pytest.fixture
def bus() -> FakeServoBus:
    return FakeServoBus({1: ServoMode.STEP, 2: ServoMode.POSITION}, {2: 1000})


@pytest.fixture
def servo_section() -> bool:
    return True


@pytest.fixture
def backend(
    webui_settings: Env, bus: FakeServoBus, servo_section: bool
) -> Iterator[Backend]:
    path = MachineConfigFile(webui_settings.config_dir).path
    path.write_text(_with_servo(path.read_text(), servo=servo_section))
    devices = Devices(
        path, webui_settings.journal_dir, audio=FakeAudioPlayer(), servo_bus=bus
    )
    backend = open_backend(webui_settings, devices=devices)
    yield backend
    backend.close()


@pytest.fixture
def manager(backend: Backend) -> Iterator[JobManager]:
    manager = job_manager(backend, backend.catalog)
    yield manager
    manager.shutdown()


class _Operator:
    """ジョブの問いに 1 つずつ答える人."""

    def __init__(self, record: JobRecord, manager: JobManager) -> None:
        self._record = record
        self._manager = manager
        self._answered: set[str] = set()
        self.messages: list[str] = []

    def wait(self) -> PromptSpec:
        """次の問いが出るまで待って返す（まだ答えない）."""
        wait_until(
            lambda: (pending := self._record.pending_prompt) is not None
            and pending[0] not in self._answered,
            timeout=30.0,
        )
        pending = self._record.pending_prompt
        assert pending is not None
        return pending[1]

    def answer(self, answer: object) -> PromptSpec:
        """次の問いに答え、その問いを返す."""
        spec = self.wait()
        pending = self._record.pending_prompt
        assert pending is not None
        self._manager.respond_prompt(pending[0], answer)
        self._answered.add(pending[0])
        self.messages.append(spec.message)
        return spec

    def start(self) -> PromptSpec:
        """ピンセットを中ほどにして「始める」を押す."""
        return self.answer(True)

    def read_range(self, bus: FakeServoBus, closed: int, opened: int) -> None:
        """手で閉じ切り・開き切りにして「読む」を押す."""
        self.wait()
        bus.turn_by_hand(2, closed)
        self.answer(True)
        self.wait()
        bus.turn_by_hand(2, opened)
        self.answer(True)

    def read_and_move(self, bus: FakeServoBus, closed: int, opened: int) -> None:
        """閉じ切り・開き切りを読ませ、手を離して「動かす」を押す."""
        self.read_range(bus, closed, opened)
        self.answer(True)


def _finish(record: JobRecord) -> None:
    wait_until(lambda: record.status.terminal, timeout=30.0)


def _move_count(bus: FakeServoBus, servo_id: int) -> int:
    """そのサーボへの移動の指令（goto と step）の数."""
    return sum(
        1 for c in bus.commands if c.servo_id == servo_id and c.kind in {"goto", "step"}
    )


class TestServoSetupJob:
    def test_success_yields_the_four_values_and_apply_writes_them(
        self, manager: JobManager, backend: Backend, bus: FakeServoBus
    ):
        record = manager.start("servo_setup", PARAMS)
        operator = _Operator(record, manager)

        start = operator.start()
        assert "中ほど" in start.message
        first = operator.answer(ROTATION)
        # 役割はボタンで選び、「もう一度動かす」は別段のボタンにする
        assert (first.kind, first.choices, first.false_label) == (
            "buttons",
            (ROTATION, OPEN_CLOSE),
            AGAIN,
        )
        assert first.default == ROTATION  # ID 1 はステップモード
        assert "ID 1" in first.message
        second = operator.answer(OPEN_CLOSE)
        assert second.default == OPEN_CLOSE
        operator.read_range(bus, 1000, 1300)
        move = operator.answer(True)
        assert "手を離して" in move.message
        operator.answer(True)  # 確認の開閉に「はい」
        _finish(record)

        assert record.status == JobStatus.SUCCEEDED, record.error
        payload = manager.apply_payload()
        assert payload.values == {
            "pnp.servo.rotation_id": 1,
            "pnp.servo.open_close_id": 2,
            "pnp.servo.closed_position": 1000,
            "pnp.servo.open_position": 1300,
        }
        assert record.result is not None and record.result.summary is not None
        for value in ("ID 1", "ID 2", "1000", "1300"):
            assert value in record.result.summary
        # 確認の開閉は閉じ切り → 開き切り
        assert bus.commands[-2:] == [
            ServoCommand(2, "goto", 1000),
            ServoCommand(2, "goto", 1300),
        ]

        backend.machine_config.write(payload.values)
        servo = backend.machine_config.machine().pnp.servo  # type: ignore[union-attr]
        assert servo is not None
        assert (
            servo.rotation_id,
            servo.open_close_id,
            servo.closed_position,
            servo.open_position,
        ) == (1, 2, 1000, 1300)
        assert servo.port == "/dev/ttyUSB0"

    def test_same_role_twice_asks_again(self, manager: JobManager, bus: FakeServoBus):
        record = manager.start("servo_setup", PARAMS)
        operator = _Operator(record, manager)

        operator.start()
        operator.answer(ROTATION)
        operator.answer(ROTATION)
        again = operator.answer(ROTATION)
        assert "ID 1" in again.message
        operator.answer(OPEN_CLOSE)
        operator.read_and_move(bus, 1000, 1300)
        operator.answer(True)
        _finish(record)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert "同じ役割" in "\n".join(record.log_lines)

    def test_wiggle_again_moves_the_servo_again(
        self, manager: JobManager, bus: FakeServoBus
    ):
        record = manager.start("servo_setup", PARAMS)
        operator = _Operator(record, manager)

        operator.start()
        operator.wait()
        once = _move_count(bus, 1)
        operator.answer(AGAIN)
        operator.wait()

        assert _move_count(bus, 1) == 2 * once
        manager.request_abort()
        _finish(record)

    def test_wiggle_degrees_sets_how_far_the_servo_swings(
        self, manager: JobManager, bus: FakeServoBus
    ):
        record = manager.start("servo_setup", {**PARAMS, "wiggle_degrees": 10.0})
        operator = _Operator(record, manager)

        operator.start()
        prompt = operator.wait()

        assert "±10°" in prompt.message
        # ID 1 はステップモード。10° = 114 step を +、-2 倍、+ で振って元へ戻す
        assert [c.value for c in bus.commands if c.kind == "step"] == [114, -228, 114]
        manager.request_abort()
        _finish(record)

    def test_freshly_installed_servos_get_the_mode_of_their_role(
        self, manager: JobManager, bus: FakeServoBus
    ):
        bus.set_mode(1, ServoMode.POSITION)  # 取り付けたばかりで 2 台とも位置モード
        record = manager.start("servo_setup", PARAMS)
        operator = _Operator(record, manager)

        operator.start()
        operator.answer(ROTATION)
        operator.answer(OPEN_CLOSE)
        operator.read_and_move(bus, 1000, 1300)
        operator.answer(True)
        _finish(record)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert (bus.mode(1), bus.mode(2)) == (ServoMode.STEP, ServoMode.POSITION)
        assert "ID 1 のモードを STEP（3）に書きました" in record.log_lines

    def test_swapped_roles_rewrite_both_modes(
        self, manager: JobManager, bus: FakeServoBus
    ):
        record = manager.start("servo_setup", PARAMS)
        operator = _Operator(record, manager)

        operator.start()
        operator.answer(OPEN_CLOSE)
        operator.answer(ROTATION)
        operator.wait()  # Open Close（ID 1）の閉じ切りを読む問い

        assert (bus.mode(1), bus.mode(2)) == (ServoMode.POSITION, ServoMode.STEP)
        assert {
            "ID 2 のモードを STEP（3）に書きました",
            "ID 1 のモードを POSITION（0）に書きました",
        } <= set(record.log_lines)
        manager.request_abort()
        _finish(record)

    def test_unusable_range_reads_again(self, manager: JobManager, bus: FakeServoBus):
        record = manager.start("servo_setup", PARAMS)
        operator = _Operator(record, manager)

        operator.start()
        operator.answer(ROTATION)
        operator.answer(OPEN_CLOSE)
        operator.read_range(bus, 1000, 1010)  # 1° 未満
        operator.read_and_move(bus, 1300, 1000)
        operator.answer(True)
        _finish(record)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert "開き量" in "\n".join(record.log_lines)
        assert manager.apply_payload().values["pnp.servo.closed_position"] == 1300

    def test_redo_on_the_check_reads_the_range_again(
        self, manager: JobManager, bus: FakeServoBus
    ):
        record = manager.start("servo_setup", PARAMS)
        operator = _Operator(record, manager)

        operator.start()
        operator.answer(ROTATION)
        operator.answer(OPEN_CLOSE)
        operator.read_and_move(bus, 1000, 1300)
        operator.answer(False)  # やり直す
        operator.read_and_move(bus, 1000, 1200)
        operator.answer(True)
        _finish(record)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert manager.apply_payload().values["pnp.servo.open_position"] == 1200

    def test_torque_is_back_on_after_reading(
        self, manager: JobManager, bus: FakeServoBus
    ):
        record = manager.start("servo_setup", PARAMS)
        operator = _Operator(record, manager)

        operator.start()
        operator.answer(ROTATION)
        operator.answer(OPEN_CLOSE)
        operator.read_range(bus, 1000, 1300)
        operator.wait()

        with pytest.raises(ServoError, match="トルク"):
            bus.turn_by_hand(2, 1100)
        manager.request_abort()
        _finish(record)

    def test_jammed_check_releases_the_torque_and_reads_again(
        self, manager: JobManager, bus: FakeServoBus
    ):
        record = manager.start("servo_setup", PARAMS)
        operator = _Operator(record, manager)

        operator.start()
        operator.answer(ROTATION)
        operator.answer(OPEN_CLOSE)
        operator.read_range(bus, 1000, 1300)
        operator.wait()
        bus.jam(2)  # 確認の開閉で当たりに押し付けて止まりきらない
        before = len(bus.commands)
        operator.answer(True)  # 動かす
        reread = operator.wait()  # 待ち時間は確認の開閉のタイムアウト（6 秒）

        assert not record.status.terminal
        assert "閉じ切り" in reread.message
        assert "トルクを切りました" in "\n".join(record.log_lines)
        # 閉じ切りへの移動が終わらないので、開き切りへは動かさずにトルクを切る
        check = bus.commands[before:]
        assert check[:2] == [
            ServoCommand(2, "goto", 1000),
            ServoCommand(2, "torque", 0),
        ]
        assert ServoCommand(2, "goto", 1300) not in check
        bus.turn_by_hand(2, 1100)  # トルクが切れているので手で回せる
        manager.request_abort()
        _finish(record)

    @pytest.mark.parametrize(
        "servo_section", [False]
    )  # [pnp] だけで [pnp.servo] が無い
    def test_without_pnp_servo_fails(self, manager: JobManager):
        record = manager.start("servo_setup", PARAMS)
        _finish(record)

        assert record.status == JobStatus.FAILED
        assert record.error is not None and "pnp.servo" in record.error

    def test_other_than_two_servos_fails(self, manager: JobManager):
        record = manager.start("servo_setup", {"max_id": 1})
        _finish(record)

        assert record.status == JobStatus.FAILED
        assert record.error is not None and "2 台" in record.error


class TestRegistration:
    def test_is_not_a_machine_job(self, backend: Backend):
        assert backend.catalog.get("servo_setup").uses_machine is False
