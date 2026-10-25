"""generate_grid_pcb ジョブの仕様テスト（実 pcbnew・装置非使用）."""

from __future__ import annotations

from pcbasm.kicad import load_board
from tests.support.polling import wait_until
from web.api.env import Env
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobStatus


class TestGenerateGridPcb:
    """generate_grid_pcb（実 pcbnew・装置非使用）."""

    def test_output_is_readable_with_divisions_squared_pads(
        self,
        manager: JobManager,
        fake_camera_settings: Env,
    ):
        record = manager.start(
            "generate_grid_pcb",
            {"size": 30.0, "divisions": 2, "pad_size": 1.0},
        )
        # 実 pcbnew 読込は Raspberry Pi では数十秒かかり得る
        wait_until(lambda: record.status.terminal, timeout=120.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        outputs = [a for a in record.result.artifacts if a.path.endswith(".kicad_pcb")]
        assert len(outputs) == 1

        pcb = load_board(fake_camera_settings.webui_data_dir / outputs[0].path)
        assert len(pcb.pads) == 4
