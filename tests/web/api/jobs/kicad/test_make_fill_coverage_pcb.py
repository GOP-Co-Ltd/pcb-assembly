"""make_fill_coverage_pcb ジョブの仕様テスト（実 pcbnew）."""

from __future__ import annotations

from pcbasm.kicad import load_board
from tests.support.polling import wait_until
from web.api.env import Env
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobStatus

# 実 pcbnew 読込は Raspberry Pi では数十秒かかり得る
_JOB_TIMEOUT = 120.0


class TestMakeFillCoveragePcb:
    """make_fill_coverage_pcb（実 pcbnew）."""

    def test_output_is_readable_by_pcbfile(
        self,
        manager: JobManager,
        fake_camera_settings: Env,
    ):
        record = manager.start("make_fill_coverage_pcb", {})
        wait_until(lambda: record.status.terminal, timeout=_JOB_TIMEOUT)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        outputs = [a for a in record.result.artifacts if a.path.endswith(".kicad_pcb")]
        assert len(outputs) == 1

        pcb = load_board(fake_camera_settings.webui_data_dir / outputs[0].path)
        assert len(pcb.pads) > 0
