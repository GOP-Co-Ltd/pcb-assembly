"""機体の機能で分かれる UI の仕様.

タブ・サイドバー・着地点は、backend が申告する機体の機能のページだけで組む。
はんだ塗布機は機能で分ける前と同じ画面になり、部品実装だけの機体は部品実装タブに着く。
"""

import re

import pytest
from fastapi.testclient import TestClient

from tests.support.machine_toml import pnp_only
from tests.support.web import BACKEND_MACHINE_ID
from web.api.env import Env as ApiEnv

BASE = f"/m/{BACKEND_MACHINE_ID}"
_TAB = re.compile(r'<a href="[^"]*" class="tab[^"]*"[^>]*>([^<]+)</a>')


def _tabs(text: str) -> list[str]:
    """トップバーのタブの表示名（機体一覧を含む）."""
    nav = text.split('data-testid="top-tabs"', 1)[1].split("</nav>", 1)[0]
    return _TAB.findall(nav)


@pytest.fixture
def client(frontend_client: TestClient) -> TestClient:
    return frontend_client


class TestSolderMachine:
    """はんだ塗布機（001 と同じ構成）の画面は、機能で分ける前と同じ."""

    def test_lands_on_the_paste_page(self, client: TestClient):
        response = client.get(BASE, follow_redirects=False)

        assert response.headers["location"] == f"{BASE}/paste_solder"

    def test_tabs(self, client: TestClient):
        text = client.get(f"{BASE}/paste_solder").text

        assert _tabs(text) == ["塗布", "校正", "基板", "機体", "機体一覧"]

    def test_pnp_page_leads_to_the_landing(self, client: TestClient):
        response = client.get(f"{BASE}/placement", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == f"{BASE}/paste_solder"


class TestPnpMachine:
    """部品実装だけの機体（machine.toml に [pnp] だけがある）."""

    @pytest.fixture
    def backend_settings(self, backend_settings: ApiEnv) -> ApiEnv:
        path = backend_settings.config_dir / "machine.toml"
        path.write_text(pnp_only(path.read_text()))
        return backend_settings

    def test_lands_on_the_pnp_page(self, client: TestClient):
        response = client.get(BASE, follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == f"{BASE}/placement"

    def test_pnp_page_is_a_placeholder(self, client: TestClient):
        text = client.get(f"{BASE}/placement").text

        assert 'data-testid="pnp-placeholder"' in text
        assert _tabs(text) == ["部品実装", "校正", "基板", "機体", "機体一覧"]

    @pytest.mark.parametrize("slug", ["paste_solder", "nozzle_cap", "paste_test_board"])
    def test_solder_pages_lead_to_the_landing(self, client: TestClient, slug: str):
        response = client.get(f"{BASE}/{slug}", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == f"{BASE}/placement"

    def test_sidebar_lists_only_the_pages_of_this_machine(self, client: TestClient):
        text = client.get(f"{BASE}/commissioning").text
        sidebar = text.split('data-testid="sidebar"', 1)[1].split("</aside>", 1)[0]

        assert f'href="{BASE}/camera_calibration"' in sidebar
        assert f'href="{BASE}/nozzle_cap"' not in sidebar
        assert f'href="{BASE}/dispense_calibration"' not in sidebar

    def test_machine_control_has_no_cap_button(self, client: TestClient):
        text = client.get(f"{BASE}/klipper_status").text

        assert 'id="mc-focus-z"' in text
        assert "mc-move-to-cap" not in text

    def test_common_pages_render(self, client: TestClient):
        for slug in (
            "commissioning",
            "camera_preview",
            "machine_config",
            "machine_check",
        ):
            assert client.get(f"{BASE}/{slug}").status_code == 200, slug
