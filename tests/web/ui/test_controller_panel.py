"""マシン操作パネルのコントローラ操作と、画面の仮想スティック（SSR）.

コントローラ操作は部品実装タブのページではなく、マシン操作パネルの「コントローラ操作」ボタンから始める。

- 部品実装機では、どの機能のページのパネルにもボタンを出す。ボタンが始めるジョブは、新ファームの機体
  （machine-info の ``stage`` が "motion"）なら gamepad_motion、Klipper の機体なら gamepad_control
- 新ファームの機体だけ、ボタンの下に XY パッドと Z スライダーを描く
- はんだ塗布だけの機体には出さない
- 部品実装タブのサイドバーにコントローラ操作のページを出さない

押したときに速度指令が届くことと、送信が途切れたら止まることは tests/e2e/test_virtual_stick_browser.py が実ブラウザで確かめる。
"""

import pytest
from fastapi.testclient import TestClient

from tests.support.machine_toml import pnp_only
from tests.support.web import BACKEND_MACHINE_ID, use_motion_machine
from web.api.env import Env as ApiEnv

BASE = f"/m/{BACKEND_MACHINE_ID}"


def _panel(page: str) -> str:
    return page.split('data-testid="machine-control-sidebar"', 1)[1].split(
        "</aside>", 1
    )[0]


def _sidebar(page: str) -> str:
    return page.split('data-testid="sidebar"', 1)[1].split("</aside>", 1)[0]


class TestMotionMachine:
    @pytest.fixture
    def backend_settings(self, backend_settings: ApiEnv) -> ApiEnv:
        # 機体の機能とジョブは backend を組んだときの machine.toml で決まるので、組む前に書き換える
        use_motion_machine(backend_settings.config_dir)
        return backend_settings

    @pytest.fixture(params=["placement", "klipper_status"])
    def page(self, frontend_client: TestClient, request: pytest.FixtureRequest) -> str:
        response = frontend_client.get(f"{BASE}/{request.param}")
        assert response.status_code == 200
        return response.text

    def test_button_starts_the_xyz_and_tweezer_job(self, page: str):
        assert 'data-job-name="gamepad_motion"' in _panel(page)
        assert 'data-job-name="gamepad_control"' not in _panel(page)

    @pytest.mark.parametrize("testid", ["virtual-stick", "vs-pad", "vs-z"])
    def test_panel_has_the_virtual_stick(self, page: str, testid: str):
        assert f'data-testid="{testid}"' in _panel(page)

    def test_loads_the_virtual_stick_script(self, page: str):
        assert "js/pnp/virtual_stick.js" in page

    def test_sidebar_has_no_controller_page(self, page: str):
        assert "gamepad_" not in _sidebar(page)


class TestKlipperPnpMachine:
    @pytest.fixture
    def backend_settings(self, backend_settings: ApiEnv) -> ApiEnv:
        path = backend_settings.config_dir / "machine.toml"
        path.write_text(pnp_only(path.read_text()))
        return backend_settings

    @pytest.fixture
    def page(self, frontend_client: TestClient) -> str:
        response = frontend_client.get(f"{BASE}/placement")
        assert response.status_code == 200
        return response.text

    def test_button_starts_the_tweezer_job(self, page: str):
        assert 'data-job-name="gamepad_control"' in _panel(page)

    def test_panel_has_no_virtual_stick(self, page: str):
        assert 'id="virtual-stick"' not in page

    def test_sidebar_has_no_controller_page(self, page: str):
        assert "gamepad_" not in _sidebar(page)


class TestSolderMachine:
    def test_panel_has_no_controller_button(self, frontend_client: TestClient):
        page = frontend_client.get(f"{BASE}/paste_solder").text

        assert 'data-testid="mc-controller"' not in page
