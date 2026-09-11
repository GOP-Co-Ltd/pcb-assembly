"""`web.api.jobs.dev` の仕様テスト（integration-with-fakes）.

計画書 webui-phase3.md「src/webui/jobs/dev.py」節が契約。実 PcbFile /
実 pcbnew / 実 matplotlib を使い、default_catalog のジョブを manager 経由で
実行して成果物を検証する:

- extract_pcb: SUCCEEDED + artifacts 5 件（PNG 1 + データ 4）、PNG は cv2 で読める
- make_fill_coverage_pcb: 出力が PcbFile で読める

（generate_grid_pcb は位置合わせタブ → test_posctrl.py へ移設済み）
- job_demo: prompt 2 回の往復で SUCCEEDED + apply payload（canny_low=応答値）/
  fail=True で FAILED / command_phase で jog エコー → quit
- requires_pcb ジョブを PCB 未選択で start → ValueError
"""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from pcbasm.pcb import PcbFile
from tests.helpers import FakeAudioPlayer
from web.api.jobs.catalog import default_catalog
from web.api.jobs.manager import JobManager, JobRecord, JobStatus
from web.api.settings import Settings
from web.api.state import AppState

from .conftest import ManagerFactory, WaitUntil, answer_next_prompt

# matplotlib 描画 + 実 pcbnew 読込は Raspberry Pi では数十秒かかり得る
_JOB_TIMEOUT = 120.0


@pytest.fixture
def dev_manager(make_manager: ManagerFactory) -> JobManager:
    return make_manager(default_catalog())


def _artifact_file(settings: Settings, path: str) -> Path:
    """Artifact.path（data/webui からの相対）を実ファイルパスに解決する."""
    return settings.webui_data_dir / path


def _answer_demo_prompts(
    record: JobRecord, manager: JobManager, *, number_answer: float
) -> None:
    """job_demo の prompt 2 回（confirm → number）へ順に応答する."""
    answered: set[str] = set()
    answer_next_prompt(record, manager, True, answered)
    answer_next_prompt(record, manager, number_answer, answered)


class TestExtractPcb:
    """extract_pcb（実 PcbFile + render_pcb）."""

    def test_extract_pcb_produces_five_artifacts(
        self,
        dev_manager: JobManager,
        state: AppState,
        real_pcb_path: Path,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        state.select_pcb(real_pcb_path)

        record = dev_manager.start("extract_pcb", {})
        wait_until(lambda: record.status.terminal, timeout=_JOB_TIMEOUT)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        artifacts = record.result.artifacts
        assert len(artifacts) == 5

        images = [a for a in artifacts if a.kind == "image"]
        files = [a for a in artifacts if a.kind == "file"]
        assert len(images) == 1
        assert len(files) == 4

        png = _artifact_file(fake_camera_settings, images[0].path)
        decoded = cv2.imread(str(png))
        assert decoded is not None
        assert decoded.size > 0

        for artifact in files:
            assert _artifact_file(fake_camera_settings, artifact.path).is_file()


class TestMakeFillCoveragePcb:
    """make_fill_coverage_pcb（実 pcbnew）."""

    def test_output_is_readable_by_pcbfile(
        self,
        dev_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        record = dev_manager.start("make_fill_coverage_pcb", {})
        wait_until(lambda: record.status.terminal, timeout=_JOB_TIMEOUT)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        outputs = [a for a in record.result.artifacts if a.path.endswith(".kicad_pcb")]
        assert len(outputs) == 1

        pcb = PcbFile(_artifact_file(fake_camera_settings, outputs[0].path))
        assert len(pcb.pads) > 0


class TestJobDemo:
    """job_demo（prompt / abort / command / Apply の E2E 用スタブ）."""

    def test_prompt_round_trip_succeeds_with_apply_payload(
        self, dev_manager: JobManager, wait_until: WaitUntil
    ):
        record = dev_manager.start("job_demo", {"steps": 1, "interval": 0.01})

        _answer_demo_prompts(record, dev_manager, number_answer=60.0)
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.apply_available is True
        assert record.result is not None
        assert record.result.apply is not None
        assert record.result.apply.values == {
            "paste_dispenser.pad_align.canny_low": 60.0
        }

    def test_fail_param_marks_failed(
        self, dev_manager: JobManager, wait_until: WaitUntil
    ):
        record = dev_manager.start(
            "job_demo", {"steps": 1, "interval": 0.01, "fail": True}
        )
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.FAILED
        assert record.error

    def test_command_phase_echoes_jog_and_quits(
        self, dev_manager: JobManager, wait_until: WaitUntil
    ):
        record = dev_manager.start(
            "job_demo", {"steps": 1, "interval": 0.01, "command_phase": True}
        )
        _answer_demo_prompts(record, dev_manager, number_answer=60.0)
        wait_until(lambda: record.status == JobStatus.RUNNING)

        dev_manager.submit_command({"type": "jog", "axis": "x", "dist": 0.1})
        wait_until(lambda: any("jog" in line for line in record.log_lines))

        dev_manager.submit_command({"type": "quit"})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED, record.error

    def test_command_phase_notifies_the_operator_before_waiting(
        self, make_manager: ManagerFactory, wait_until: WaitUntil
    ):
        """コマンド待ちに入る前に機体スピーカーが作業者を呼び戻す.

        prompt を出さないオペレータ待ち（手動ペーストローディング・吐出量キャリブの
        メニューと同じ形）が通知されることを、Klipper 不要な job_demo でピンする。 prompt 2 回ぶんに続く 3
        回目が、コマンド待ち入口の通知。
        """
        player = FakeAudioPlayer()
        manager = make_manager(default_catalog(), audio_player=player)
        record = manager.start(
            "job_demo", {"steps": 1, "interval": 0.01, "command_phase": True}
        )
        _answer_demo_prompts(record, manager, number_answer=60.0)
        wait_until(lambda: len(player.played) == 3)

        assert [sound for sound, _ in player.played] == ["prompt"] * 3

        manager.submit_command({"type": "quit"})
        wait_until(lambda: record.status.terminal)
        assert record.status == JobStatus.SUCCEEDED, record.error

    def test_requires_pcb_job_without_selection_raises_value_error(
        self, dev_manager: JobManager
    ):
        with pytest.raises(ValueError):
            dev_manager.start("extract_pcb", {})
