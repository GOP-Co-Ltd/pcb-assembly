"""新ファームの機体（``[pnp.motion]``）のマシン操作パネルと、Klipper に依存する項目の出し分け（SSR）.

frontend は backend の ``machine-info`` の ``stage`` だけを見て項目を切り替える。

- 新ファームの機体では、焦点 Z・キャップ位置・G-code の欄・Mainsail へのリンクを出さない
- 新ファームの機体だけ、ロードセルの荷重の行と TARE ボタンを隠して置く（JS が load_label を見て出す）
- Z バーは + を上に並べる（Z の原点は上で、上昇が Z の増加）
- 再起動（復旧）の確認文は MCU の再起動を伝える（WebUI のサービスは再起動しない）
- Klipper の機体はこれまでどおり（Z バーは − が上）

押したときの動きは tests/e2e/test_motion_panel_browser.py が実ブラウザで確かめる。
"""

import re

import pytest
from fastapi.testclient import TestClient

from tests.support.web import use_motion_machine
from web.api.env import Env as ApiEnv

_KLIPPER_ONLY_IDS = ("mc-focus-z", "mc-move-to-cap", "ks-gcode-form")


def _z_bar(page: str) -> list[str]:
    return re.findall(r'data-mc-jog="z" data-mc-dist="([^"]+)"', page)


class TestMotionMachinePanel:
    @pytest.fixture
    def backend_settings(self, backend_settings: ApiEnv) -> ApiEnv:
        # 機体の機能は backend を組んだときの machine.toml で決まるので、組む前に書き換える
        use_motion_machine(backend_settings.config_dir)
        return backend_settings

    @pytest.fixture
    def page(self, frontend_client: TestClient) -> str:
        response = frontend_client.get("/klipper_status")
        assert response.status_code == 200
        return response.text

    @pytest.mark.parametrize("element_id", _KLIPPER_ONLY_IDS)
    def test_klipper_only_controls_are_hidden(self, page: str, element_id: str):
        assert f'id="{element_id}"' not in page

    def test_mainsail_link_is_hidden(self, page: str):
        assert "Mainsail" not in page

    def test_z_bar_puts_plus_on_top(self, page: str):
        assert _z_bar(page) == ["10", "1", "0.1", "-0.1", "-1", "-10"]

    def test_load_row_and_tare_wait_hidden_for_the_load_label(self, page: str):
        # 荷重の行は backend が load_label を返したときだけ JS が出す
        assert re.search(r'<div id="mc-load-row"[^>]*\bhidden\b', page)
        assert 'id="mc-tare"' in page

    def test_firmware_restart_confirms_the_mcu_restart(self, page: str):
        [confirm] = re.findall(
            r'data-testid="firmware-restart"[^>]*data-confirm="([^"]+)"', page
        )
        assert "MCU" in confirm
        assert "WebUI" not in confirm


class TestKlipperMachinePanel:
    @pytest.fixture
    def page(self, frontend_client: TestClient) -> str:
        response = frontend_client.get("/klipper_status")
        assert response.status_code == 200
        return response.text

    @pytest.mark.parametrize("element_id", _KLIPPER_ONLY_IDS)
    def test_klipper_controls_are_shown(self, page: str, element_id: str):
        assert f'id="{element_id}"' in page

    def test_load_row_and_tare_are_absent(self, page: str):
        assert 'id="mc-load-row"' not in page
        assert 'id="mc-tare"' not in page

    def test_z_bar_puts_minus_on_top(self, page: str):
        assert _z_bar(page) == ["-10", "-1", "-0.1", "0.1", "1", "10"]
