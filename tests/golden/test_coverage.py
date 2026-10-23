"""ジョブの golden の網羅性（装置を動かすジョブはどれも、送信列が golden で固定されている）."""

import pytest

from tests.golden.conftest import JOBS
from tests.golden.scenarios import SCENARIOS, scenarios_of
from web.api.jobs.catalog import default_catalog


class TestCoverage:
    def test_every_machine_job_has_scenario(self):
        """装置を動かすジョブを catalog に足したら、そのシナリオも表に足す."""
        jobs = {
            definition.name
            for definition in default_catalog().list()
            if definition.uses_machine
        }

        assert sorted(jobs - {scenario.job for scenario in SCENARIOS}) == []

    @pytest.mark.parametrize(
        "job", ["paste_solder", "board_tour", "paste_volume_calibration"]
    )
    def test_job_has_success_scenario(self, job: str):
        """日常の制作と校正で通る成功経路は、分岐の golden とは別に必ず持つ."""
        assert any(scenario.succeeds for scenario in scenarios_of(job))

    def test_every_scenario_has_golden(self):
        """表にあってテストが採っていないシナリオで、網羅したように見せない."""
        names = {f"{scenario.name}.gcode" for scenario in SCENARIOS}

        assert names == {path.name for path in JOBS.glob("*.gcode")}
