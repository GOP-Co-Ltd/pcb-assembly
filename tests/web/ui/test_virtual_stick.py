"""XYZ のコントローラ操作のページ（gamepad_motion）と、画面の仮想スティック（SSR）.

- 新ファームの機体（machine-info の ``stage`` が "motion"）では、部品実装タブに出し、ジョブのフォームと
  コンソールに加えて、XY パッドと Z スライダーを描く
- Klipper の機体では出さない（URL を直接開いても着地点へ戻す）

押したときに速度指令が届くことと、送信が途切れたら止まることは tests/e2e/test_virtual_stick_browser.py が実ブラウザで確かめる。
"""

import pytest
from fastapi.testclient import TestClient

from tests.support.machine_toml import pnp_only
from tests.support.web import BACKEND_MACHINE_ID, use_motion_machine
from web.api.env import Env as ApiEnv

BASE = f"/m/{BACKEND_MACHINE_ID}"


class TestMotionMachine:
    @pytest.fixture
    def backend_settings(self, backend_settings: ApiEnv) -> ApiEnv:
        # 機体の機能とジョブは backend を組んだときの machine.toml で決まるので、組む前に書き換える
        use_motion_machine(backend_settings.config_dir)
        return backend_settings

    @pytest.fixture
    def page(self, frontend_client: TestClient) -> str:
        response = frontend_client.get(f"{BASE}/gamepad_motion")
        assert response.status_code == 200
        return response.text

    @pytest.mark.parametrize(
        "testid", ["job-form", "job-console", "vs-pad", "vs-z", "virtual-stick"]
    )
    def test_page_has_the_job_and_the_virtual_stick(self, page: str, testid: str):
        assert f'data-testid="{testid}"' in page

    def test_virtual_stick_needs_the_control(self, page: str):
        section = page.split('id="virtual-stick"', 1)[1].split(">", 1)[0]

        assert "data-requires-control" in section

    def test_loads_the_virtual_stick_script(self, page: str):
        assert "js/pnp/virtual_stick.js" in page

    def test_sidebar_lists_the_page(self, frontend_client: TestClient):
        text = frontend_client.get(f"{BASE}/placement").text
        sidebar = text.split('data-testid="sidebar"', 1)[1].split("</aside>", 1)[0]

        assert f'href="{BASE}/gamepad_motion"' in sidebar


class TestKlipperPnpMachine:
    @pytest.fixture
    def backend_settings(self, backend_settings: ApiEnv) -> ApiEnv:
        path = backend_settings.config_dir / "machine.toml"
        path.write_text(pnp_only(path.read_text()))
        return backend_settings

    def test_page_leads_to_the_landing(self, frontend_client: TestClient):
        response = frontend_client.get(f"{BASE}/gamepad_motion", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == f"{BASE}/placement"

    def test_sidebar_does_not_list_the_page(self, frontend_client: TestClient):
        text = frontend_client.get(f"{BASE}/placement").text
        sidebar = text.split('data-testid="sidebar"', 1)[1].split("</aside>", 1)[0]

        assert f'href="{BASE}/gamepad_motion"' not in sidebar
        assert f'href="{BASE}/gamepad_control"' in sidebar
