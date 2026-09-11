"""テスト塗布基板ページとbackend中継のfrontend結合テスト."""

import re

from fastapi.testclient import TestClient

_PAGE = "/m/uitest/pasting/paste_test_board"
_API = "/m/uitest/api/pasting/paste-test-board"


class TestPasteTestBoardPage:
    def test_renders_dedicated_configuration_and_preview_page(
        self, frontend_client: TestClient
    ):
        """共通の feature.html ではなく専用テンプレートが選ばれている.

        テーブル編集・プレビュー追従・draft 復元・ソート・export の実操作は e2e が持つ。
        """
        response = frontend_client.get(_PAGE)

        assert response.status_code == 200
        text = response.text
        assert "テスト塗布基板生成" in text
        assert 'data-testid="ptb-pattern-table"' in text
        assert 'data-testid="ptb-preview"' in text
        # 流量計測パッドの設定欄（大きさと個数）
        assert 'id="ptb-flow-size"' in text
        assert 'id="ptb-flow-count"' in text
        assert "js/paste_test_board.js?v=" in text

    def test_page_actions_do_not_require_machine_control(
        self, frontend_client: TestClient
    ):
        text = frontend_client.get(_PAGE).text
        for element_id in ("ptb-import", "ptb-export", "ptb-generate"):
            tag = re.search(rf'<button[^>]*id="{element_id}"[^>]*>', text)
            assert tag is not None
            assert "data-requires-control" not in tag.group(0)


class TestPasteTestBoardProxy:
    def test_options_are_available_through_machine_prefix(
        self, frontend_client: TestClient
    ):
        response = frontend_client.get(f"{_API}/options")

        assert response.status_code == 200
        assert response.json()["kind"] == "paste_test_board"

    def test_binary_download_headers_pass_through_unchanged(
        self, frontend_client: TestClient
    ):
        config = frontend_client.get(f"{_API}/options").json()["config"]

        response = frontend_client.post(f"{_API}/generate", json=config)

        assert response.status_code == 200
        assert response.content.startswith(b"(kicad_pcb")
        assert response.headers["content-disposition"] == (
            'attachment; filename="pcbasm-paste-test-board.kicad_pcb"'
        )
