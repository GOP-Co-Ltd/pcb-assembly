"""load_cell_calibrate ジョブの仕様テスト（新ファームの機体、FakeMotionStage）.

- 何も載せずにタール → 既知の重さを載せて「測る」→ 重さ [g] を入力 → counts_per_gram を出す
- 結果の Apply の値は [pnp.motion.load_cell] の counts_per_gram で、設定ページと同じ書き込みで machine.toml に入る
- 重さの差が小さすぎれば、理由をログに出して載せるところからやり直す
- 差が負（押す向きと逆の荷重）でも絶対値で求め、ログに符号の意味を出す
- ジョブの間は自動のタールを止め、終われば（失敗でも）戻す
- 測る途中でロードセルが読めなければ、理由をログに出して載せるところからやり直す（shutdown なら失敗）
- ロードセルの無い機体と、接触保持の間は、理由を出して失敗で終わる
- 実機での確認は受け入れ試験（docs/acceptance.md の A36）
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pcbasm.hal import ContactSettings, Velocity
from pcbasm.hal.fake import FakeAudioPlayer, FakeMotionStage
from pcbasm.machine.devices import Devices
from pcbasm.pnp.load_cell_calibration import COUNTS_PER_GRAM_KEY
from tests.support.machine_toml import motion_table, pnp_motion
from tests.support.polling import wait_until
from tests.support.web import fake_motion_stage, job_manager
from web.api.backend import Backend, open_backend
from web.api.env import Env
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobRecord, JobStatus, PromptSpec
from web.api.machine_config import InvalidSettingError, MachineConfigFile

CURRENT = 2666.98


@pytest.fixture
def stage() -> FakeMotionStage:
    return fake_motion_stage(homed=True, load_cell=True, counts_per_gram=2000.0)


@pytest.fixture
def load_cell_section() -> bool:
    return True


@pytest.fixture
def backend(
    webui_settings: Env,
    audio_player: FakeAudioPlayer,
    stage: FakeMotionStage,
    load_cell_section: bool,
) -> Iterator[Backend]:
    path = MachineConfigFile(webui_settings.config_dir).path
    motion = motion_table()
    if load_cell_section:
        motion["load_cell"] = {
            "sclk_pin": "gpio0",
            "dout_pin": "gpio1",
            "counts_per_gram": CURRENT,
        }
    path.write_text(pnp_motion(path.read_text(), motion))
    devices = Devices(
        path, webui_settings.journal_dir, audio=audio_player, motion=stage
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

    def answer(self, answer: object) -> PromptSpec:
        """次の問いが出るまで待って答え、その問いを返す."""
        wait_until(
            lambda: (pending := self._record.pending_prompt) is not None
            and pending[0] not in self._answered,
            timeout=30.0,
        )
        pending = self._record.pending_prompt
        assert pending is not None
        self._manager.respond_prompt(pending[0], answer)
        self._answered.add(pending[0])
        return pending[1]

    def tare(self, stage: FakeMotionStage) -> PromptSpec:
        """何も載せずに「タール」を押し、ジョブがタールを取るまで待つ."""
        spec = self.answer(True)
        wait_until(lambda: "tare" in stage.commands)
        return spec

    def measure(self, stage: FakeMotionStage, load: float, grams: float) -> None:
        """``load`` [g] を載せて「測る」を押し、重さ ``grams`` を入れる."""
        stage.set_load(load)
        measure = self.answer(True)
        assert measure.true_label == "測る"
        number = self.answer(grams)
        assert number.kind == "number"


def _finish(record: JobRecord) -> None:
    wait_until(lambda: record.status.terminal, timeout=30.0)


def _log(record: JobRecord) -> str:
    return "\n".join(record.log_lines)


class TestLoadCellCalibrateJob:
    def test_tare_then_measure_yields_counts_per_gram_and_apply_writes_it(
        self, manager: JobManager, backend: Backend, stage: FakeMotionStage
    ):
        stage.set_load(7.0)  # タール（ステージの自重など）
        record = manager.start("load_cell_calibrate", {})
        operator = _Operator(record, manager)

        tare = operator.tare(stage)
        assert tare.true_label == "タール"
        operator.measure(stage, 107.0, 100.0)
        _finish(record)

        assert record.status == JobStatus.SUCCEEDED, record.error
        payload = manager.apply_payload()
        assert payload.values == {COUNTS_PER_GRAM_KEY: 2000.0}
        assert record.result is not None and record.result.summary is not None
        assert "2000.00" in record.result.summary
        # 開いているステージは machine.toml を読み直さない
        assert "再起動" in _log(record)

        backend.machine_config.write(payload.values)
        motion = backend.machine_config.machine().pnp.motion  # type: ignore[union-attr]
        assert motion is not None and motion.load_cell is not None
        assert motion.load_cell.counts_per_gram == 2000.0
        assert motion.load_cell.sclk_pin == "gpio0"

    def test_too_small_a_change_asks_to_measure_again(
        self, manager: JobManager, stage: FakeMotionStage
    ):
        record = manager.start("load_cell_calibrate", {})
        operator = _Operator(record, manager)
        operator.tare(stage)

        operator.measure(stage, 0.1, 0.1)  # 200 counts
        operator.measure(stage, 50.0, 50.0)
        _finish(record)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert "小さすぎ" in _log(record)
        assert manager.apply_payload().values == {COUNTS_PER_GRAM_KEY: 2000.0}

    def test_a_load_against_the_pressing_direction_is_calibrated_by_its_magnitude(
        self, manager: JobManager, stage: FakeMotionStage
    ):
        record = manager.start("load_cell_calibrate", {})
        operator = _Operator(record, manager)
        operator.tare(stage)

        operator.measure(stage, -20.0, 20.0)
        _finish(record)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert manager.apply_payload().values == {COUNTS_PER_GRAM_KEY: 2000.0}
        assert "押す向きと逆" in _log(record)

    def test_auto_tare_is_suspended_from_the_tare_to_the_end(
        self, manager: JobManager, stage: FakeMotionStage
    ):
        record = manager.start("load_cell_calibrate", {})
        operator = _Operator(record, manager)
        operator.tare(stage)

        operator.measure(stage, 50.0, 50.0)
        _finish(record)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert stage.commands == ["suspend_auto_tare", "tare", "resume_auto_tare"]

    def test_auto_tare_is_resumed_when_the_job_is_aborted(
        self, manager: JobManager, stage: FakeMotionStage
    ):
        record = manager.start("load_cell_calibrate", {})
        operator = _Operator(record, manager)
        operator.tare(stage)
        wait_until(lambda: record.pending_prompt is not None)

        assert manager.request_abort()
        _finish(record)

        assert stage.commands[-1] == "resume_auto_tare"

    def test_a_bad_sample_while_measuring_asks_to_measure_again(
        self, manager: JobManager, stage: FakeMotionStage
    ):
        record = manager.start("load_cell_calibrate", {})
        operator = _Operator(record, manager)
        operator.tare(stage)

        stage.set_load_error("READ_TOO_LONG")
        operator.answer(True)  # 測る → 読めない
        wait_until(lambda: "READ_TOO_LONG" in _log(record))
        stage.set_load_error(None)
        operator.measure(stage, 50.0, 50.0)
        _finish(record)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert manager.apply_payload().values == {COUNTS_PER_GRAM_KEY: 2000.0}
        assert stage.commands.count("tare") == 1

    def test_a_shutdown_while_measuring_fails_the_job(
        self, manager: JobManager, stage: FakeMotionStage
    ):
        record = manager.start("load_cell_calibrate", {})
        operator = _Operator(record, manager)
        operator.tare(stage)

        stage.shutdown("Command request")
        operator.answer(True)
        _finish(record)

        assert record.status == JobStatus.FAILED
        assert stage.commands[-1] == "resume_auto_tare"

    def test_apply_rejects_a_value_the_setting_page_rejects(self, backend: Backend):
        """設定ページの項目検査（正の数）が、Apply の値にも効く."""
        with pytest.raises(InvalidSettingError):
            backend.machine_config.write({COUNTS_PER_GRAM_KEY: 0.0})


class TestHoldingContact:
    @pytest.fixture
    def stage(self) -> FakeMotionStage:
        return fake_motion_stage(homed=True, load_cell=True, contact=ContactSettings())

    def test_fails_with_the_reason(self, manager: JobManager, stage: FakeMotionStage):
        stage.set_velocity(Velocity(z=-1.0))  # Z− がピンセットに押し当たる向き
        stage.set_load(60.0)
        assert stage.status().contact

        record = manager.start("load_cell_calibrate", {})
        _finish(record)

        assert record.status == JobStatus.FAILED
        assert record.error is not None and "接触" in record.error
        assert "tare" not in stage.commands


class TestWithoutLoadCell:
    @pytest.fixture
    def load_cell_section(self) -> bool:
        return False

    def test_fails_with_the_reason(self, manager: JobManager, stage: FakeMotionStage):
        record = manager.start("load_cell_calibrate", {})
        _finish(record)

        assert record.status == JobStatus.FAILED
        assert record.error is not None and "ロードセル" in record.error
        assert stage.commands == []
