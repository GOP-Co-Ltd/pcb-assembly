"""機体の機能で分かれる backend の仕様（machine.toml に [pnp] だけがある部品実装機）.

部品実装だけの機体でも backend は起動し、共通のジョブと API だけを出す。

塗布のジョブと API は登録しないので、塗布の設定が無いことによる分岐はどこにも要らない。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pcbasm.commissioning import COMMON_STEPS
from tests.support.machine_toml import pnp_only


@pytest.fixture
def config_dir(config_dir: Path) -> Path:
    """部品実装だけの機体の設定ディレクトリ."""
    path = config_dir / "machine.toml"
    path.write_text(pnp_only(path.read_text()))
    return config_dir


class TestPnpMachine:
    def test_starts_without_a_config_error(self, client: TestClient):
        assert client.get("/api/session/state").json()["config_error"] is None

    def test_reports_its_capabilities(self, client: TestClient):
        info = client.get("/api/session/machine-info").json()

        assert info["capabilities"] == [{"name": "pnp", "label": "部品実装"}]

    def test_registers_only_the_common_jobs(self, client: TestClient):
        names = {job["name"] for job in client.get("/api/jobs").json()["jobs"]}

        assert names == {
            "reference_point_setup",
            "camera_calibration",
            "board_tour",
            "orthogonality_test",
            "extract_pcb",
            "generate_grid_pcb",
            "generate_rect_pcb",
            "make_fill_coverage_pcb",
            "machine_check",
        }

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/pasting/board"),
            ("POST", "/api/pasting/nozzle/cap/record"),
            ("POST", "/api/jobs/paste_solder"),
        ],
    )
    def test_has_no_solder_api(self, client: TestClient, method: str, path: str):
        assert client.request(method, path, json={}).status_code == 404

    def test_commissioning_has_only_the_common_steps(self, client: TestClient):
        steps = client.get("/api/readiness").json()["steps"]

        assert [step["name"] for step in steps] == [step.name for step in COMMON_STEPS]

    def test_settings_have_no_solder_section(self, client: TestClient):
        fields = client.get("/api/machine_config").json()["fields"]

        assert fields
        assert "solder" not in {field["section"] for field in fields}
