"""`webui.jobs.dev` の仕様テスト（integration-with-fakes）.

計画書 webui-phase3.md「src/webui/jobs/dev.py」節が契約。実 PcbFile /
実 pcbnew / 実 matplotlib を使い、default_catalog のジョブを manager 経由で
実行して成果物を検証する:

- extract_pcb: SUCCEEDED + artifacts 5 件（PNG 1 + データ 4）、PNG は cv2 で読める
- make_fill_coverage_pcb: 出力が PcbFile で読める

（fill_path_simulate は塗布タブ → test_pasting.py、generate_grid_pcb は位置合わせ
タブ → test_posctrl.py へ移設済み）
- job_demo: prompt 2 回の往復で SUCCEEDED + apply payload（canny_low=応答値）/
  fail=True で FAILED / command_phase で jog エコー → quit
- requires_pcb ジョブを PCB 未選択で start → ValueError
"""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from pcbasm.pcb import PcbFile
from webui.jobs.catalog import default_catalog
from webui.jobs.context import PromptSpec
from webui.jobs.manager import JobManager, JobStatus
from webui.settings import Settings
from webui.state import AppState

from .conftest import ManagerFactory, WaitUntil

# matplotlib 描画 + 実 pcbnew 読込は Raspberry Pi では数十秒かかり得る
_JOB_TIMEOUT = 120.0


@pytest.fixture
def dev_manager(make_manager: ManagerFactory) -> JobManager:
    return make_manager(default_catalog())


def _artifact_file(settings: Settings, path: str) -> Path:
    """Artifact.path（data/webui からの相対）を実ファイルパスに解決する."""
    return settings.data_dir / "webui" / path


def _pending_prompt(manager: JobManager) -> tuple[str, PromptSpec] | None:
    record = manager.current()
    return record.pending_prompt if record is not None else None


def _answer_prompts(
    manager: JobManager,
    wait_until: WaitUntil,
    *,
    number_answer: float,
    count: int = 2,
) -> None:
    """Pending prompt を kind に応じて count 回応答する（confirm=True, number=指定値）."""
    answered: set[str] = set()
    for _ in range(count):
        wait_until(
            lambda: (p := _pending_prompt(manager)) is not None and p[0] not in answered
        )
        pending = _pending_prompt(manager)
        assert pending is not None
        prompt_id, spec = pending
        manager.respond_prompt(
            prompt_id, True if spec.kind == "confirm" else number_answer
        )
        answered.add(prompt_id)


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

        _answer_prompts(dev_manager, wait_until, number_answer=60.0)
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
        _answer_prompts(dev_manager, wait_until, number_answer=60.0)
        wait_until(lambda: record.status == JobStatus.RUNNING)

        dev_manager.submit_command({"type": "jog", "axis": "x", "dist": 0.1})
        wait_until(lambda: any("jog" in line for line in record.log_lines))

        dev_manager.submit_command({"type": "quit"})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED, record.error

    def test_requires_pcb_job_without_selection_raises_value_error(
        self, dev_manager: JobManager
    ):
        with pytest.raises(ValueError):
            dev_manager.start("extract_pcb", {})
