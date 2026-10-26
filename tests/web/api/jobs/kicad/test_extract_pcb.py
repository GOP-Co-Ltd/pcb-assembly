"""extract_pcb ジョブの仕様テスト（integration-with-fakes）.

実 load_board / 実 matplotlib を使い、manager 経由で実行して成果物を検証する:

- SUCCEEDED + artifacts 5 件（PNG 1 + データ 4）、PNG は cv2 で読める
- PCB 未選択で start → ValueError
"""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from tests.support.polling import wait_until
from web.api.backend import Backend
from web.api.env import Env
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobStatus

# matplotlib 描画 + 実 pcbnew 読込は Raspberry Pi では数十秒かかり得る
_JOB_TIMEOUT = 120.0


def _artifact_file(settings: Env, path: str) -> Path:
    """Artifact.path（data/webui からの相対）を実ファイルパスに解決する."""
    return settings.webui_data_dir / path


class TestExtractPcb:
    """extract_pcb（実 load_board + render_pcb）."""

    def test_extract_pcb_produces_five_artifacts(
        self,
        manager: JobManager,
        camera_backend: Backend,
        real_pcb_path: Path,
        fake_camera_settings: Env,
    ):
        camera_backend.selection.select(real_pcb_path)

        record = manager.start("extract_pcb", {})
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
        self, manager: JobManager
    ):
        with pytest.raises(ValueError):
            manager.start("extract_pcb", {})
