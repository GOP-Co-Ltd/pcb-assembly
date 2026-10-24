"""装置診断ジョブが G-code を 1 行も送らないことを golden で固定する.

診断は読み取りだけで、不合格で終わっても駐機しない。
シナリオは scenarios.py の表にあり、golden は ``jobs/<シナリオ名>.gcode``（どれも空）に置く。
"""

from collections.abc import Callable

import numpy as np
import pytest

from pcbasm.hal import Camera
from pcbasm.hal.fake import FAKE_MACHINE_PRINTER_CONFIG, FakeCamera, FakeKlipper
from pcbasm.vision import Image
from tests.golden.conftest import JOBS, finish, scene_camera
from tests.golden.scenarios import Scenario, scenarios_of
from tests.support.golden import assert_transcript
from web.api.backend import Backend
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobStatus


class TestMachineCheck:
    @pytest.fixture
    def machine_klipper(self) -> FakeKlipper:
        """駐機の PRESENT マクロまでそろった、実機と同じ printer.cfg の fake 機体."""
        return FakeKlipper(FAKE_MACHINE_PRINTER_CONFIG)

    @pytest.mark.parametrize(
        "scenario", scenarios_of("machine_check", "all_ok"), ids=lambda s: s.name
    )
    def test_all_ok(
        self,
        manager: JobManager,
        job_backend: Backend,
        machine_klipper: FakeKlipper,
        mount_camera: Callable[[Camera], None],
        scenario: Scenario,
    ):
        mount_camera(scene_camera(machine_klipper, scenario.rotation))

        record = manager.start("machine_check", {})
        finish(job_backend, record)

        assert record.result is not None
        assert record.result.summary == "全 7 項目 ok"
        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")

    @pytest.mark.parametrize(
        "scenario",
        scenarios_of("machine_check", "camera_mismatch"),
        ids=lambda s: s.name,
    )
    def test_camera_mismatch(
        self,
        manager: JobManager,
        job_backend: Backend,
        machine_klipper: FakeKlipper,
        mount_camera: Callable[[Camera], None],
        scenario: Scenario,
    ):
        """カメラの解像度が校正と違えば失敗で終わり、そのときも駐機しない."""
        mount_camera(FakeCamera([Image(np.zeros((480, 640, 3), dtype=np.uint8))]))

        record = manager.start("machine_check", {})
        finish(job_backend, record, JobStatus.FAILED)

        assert record.error == "1 項目が不合格です: カメラと校正"
        assert (
            "NG  カメラと校正: カメラ 640x480 が校正 1280x800 と違います"
            in record.log_lines
        )
        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")
