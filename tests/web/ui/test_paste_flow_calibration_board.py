"""流量キャリブレーション基板ページとbackend中継のfrontend結合テスト."""

import re

from fastapi.testclient import TestClient

_PAGE = "/m/uitest/pasting/paste_flow_calibration_board"
_API = "/m/uitest/api/pasting/paste-flow-calibration-board"


class TestPasteFlowCalibrationBoardPage:
    def test_renders_dedicated_configuration_and_preview_page(
        self, frontend_client: TestClient
    ):
        response = frontend_client.get(_PAGE)

        assert response.status_code == 200
        text = response.text
        assert "はんだペースト流量キャリブレーション基板生成" in text
        assert 'id="pfc-board-width"' in text
        assert 'id="pfc-pad-gap"' in text
        assert 'id="pfc-footprint-search"' in text
        assert 'id="pfc-footprint-results"' in text
        assert 'data-testid="pfc-pattern-table"' in text
        assert 'data-testid="pfc-preview"' in text
        assert 'data-testid="pfc-generate"' in text
        assert "回転分割数" in text
        assert "繰り返し数" in text
        assert "転置配置" in text
        assert "自動最適配置" in text
        assert "任意サイズパッド" in text
        assert "編集中の設定は機体ごとにこのブラウザへ自動保存されます" in text
        assert 'data-sort-field="name"' in text
        assert 'data-sort-field="pad"' in text
        assert "名称を検索" in text
        assert ">名称</button>" in text
        assert "由来footprint" not in text
        assert "パッド種" in text
        assert "パッド間余白" in text
        assert "配置範囲外" in text
        assert "n 列" not in text
        assert "m 行" not in text
        assert "js/paste_flow_calibration_board.js?v=" in text

    def test_feature_is_listed_in_pasting_sidebar(self, frontend_client: TestClient):
        response = frontend_client.get("/m/uitest/pasting")

        assert response.status_code == 200
        assert f'href="{_PAGE}"' in response.text
        assert "はんだペースト流量キャリブレーション基板生成" in response.text

    def test_page_actions_do_not_require_machine_control(
        self, frontend_client: TestClient
    ):
        text = frontend_client.get(_PAGE).text
        for element_id in ("pfc-import", "pfc-export", "pfc-generate"):
            tag = re.search(rf'<button[^>]*id="{element_id}"[^>]*>', text)
            assert tag is not None
            assert "data-requires-control" not in tag.group(0)


class TestPasteFlowCalibrationBoardProxy:
    def test_options_are_available_through_machine_prefix(
        self, frontend_client: TestClient
    ):
        response = frontend_client.get(f"{_API}/options")

        assert response.status_code == 200
        assert response.json()["kind"] == "paste_flow_calibration_board"

    def test_binary_download_headers_pass_through_unchanged(
        self, frontend_client: TestClient
    ):
        config = frontend_client.get(f"{_API}/options").json()["config"]

        response = frontend_client.post(f"{_API}/generate", json=config)

        assert response.status_code == 200
        assert response.content.startswith(b"(kicad_pcb")
        assert response.headers["content-disposition"] == (
            'attachment; filename="pcbasm-paste-flow-calibration-board.kicad_pcb"'
        )
