"""矩形PCB生成ジョブの成果物を実KiCADデータで検証する。"""

from __future__ import annotations

import pytest

from pcbasm.pcb import PcbFile
from tests.web.api.jobs.conftest import WaitUntil
from web.api.jobs.manager import JobManager, JobStatus
from web.api.settings import Settings


class TestGenerateRectPcb:
    """generate_rect_pcb（実 pcbnew・装置非使用）."""

    def test_output_is_readable_outline_only_pcb(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        record = manager.start("generate_rect_pcb", {"width": 50.0, "height": 20.0})
        # 実 pcbnew 読込は Raspberry Pi では数十秒かかり得る
        wait_until(lambda: record.status.terminal, timeout=120.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        outputs = [a for a in record.result.artifacts if a.path.endswith(".kicad_pcb")]
        assert len(outputs) == 1

        pcb = PcbFile(fake_camera_settings.webui_data_dir / outputs[0].path)
        assert pcb.outline.width == pytest.approx(50.0, abs=0.1)
        assert pcb.outline.height == pytest.approx(20.0, abs=0.1)
        assert len(pcb.pads) == 0
        assert len(pcb.copper) == 0
