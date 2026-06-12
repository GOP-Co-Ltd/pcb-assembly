"""`webui.routers.pages` の仕様テスト.

計画書「routers」節 + spec §10:

- `GET /` → `/posctrl` へ 307 リダイレクト
- 4 タブ + 既知 feature + `/settings` が 200
- 全ページ共通の chrome（E-STOP・マシン操作パネル・mainsail console リンク）
- 未知タブ / 未知 feature → 404
"""

import pytest
from fastapi.testclient import TestClient

from webui.settings import Settings

TABS = ["dev", "pasting", "pnp", "posctrl"]


class TestPages:
    """Jinja2 ページ配信."""

    def test_root_redirects_to_posctrl(self, client: TestClient):
        response = client.get("/", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == "/posctrl"

    @pytest.mark.parametrize("tab", TABS)
    def test_tab_page_renders_with_common_chrome(
        self, client: TestClient, webui_settings: Settings, tab: str
    ):
        response = client.get(f"/{tab}")

        assert response.status_code == 200
        assert "E-STOP" in response.text
        assert "machine-control" in response.text
        assert webui_settings.mainsail_url in response.text

    def test_known_feature_page_renders(self, client: TestClient):
        response = client.get("/posctrl/reference_point_setup")

        assert response.status_code == 200

    def test_settings_page_renders(self, client: TestClient):
        response = client.get("/settings")

        assert response.status_code == 200

    def test_unknown_tab_returns_404(self, client: TestClient):
        assert client.get("/no-such-tab").status_code == 404

    def test_unknown_feature_returns_404(self, client: TestClient):
        assert client.get("/posctrl/no-such-feature").status_code == 404
