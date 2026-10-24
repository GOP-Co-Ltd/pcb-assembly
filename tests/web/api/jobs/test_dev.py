"""`web.api.jobs.dev` の仕様テスト（integration-with-fakes）.

計画書 webui-phase3.md「src/webui/jobs/dev.py」節が契約。実 load_board /
実 pcbnew / 実 matplotlib を使い、default_catalog のジョブを manager 経由で
実行して成果物を検証する:

- extract_pcb: SUCCEEDED + artifacts 5 件（PNG 1 + データ 4）、PNG は cv2 で読める
- extract_pcb を PCB 未選択で start → ValueError
- make_fill_coverage_pcb: 出力が load_board で読める
"""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from pcbasm.kicad import load_board
from web.api.env import Settings
from web.api.jobs.catalog import default_catalog
from web.api.jobs.manager import JobManager, JobStatus
from web.api.state import AppState

from .conftest import ManagerFactory, WaitUntil

# matplotlib 描画 + 実 pcbnew 読込は Raspberry Pi では数十秒かかり得る
_JOB_TIMEOUT = 120.0


@pytest.fixture
def dev_manager(make_manager: ManagerFactory) -> JobManager:
    return make_manager(default_catalog())


def _artifact_file(settings: Settings, path: str) -> Path:
    """Artifact.path（data/webui からの相対）を実ファイルパスに解決する."""
    return settings.webui_data_dir / path


class TestExtractPcb:
    """extract_pcb（実 load_board + render_pcb）."""

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

    def test_requires_pcb_job_without_selection_raises_value_error(
        self, dev_manager: JobManager
    ):
        with pytest.raises(ValueError):
            dev_manager.start("extract_pcb", {})


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

        pcb = load_board(_artifact_file(fake_camera_settings, outputs[0].path))
        assert len(pcb.pads) > 0
