"""一括管理ページ（``/bulk``）の仕様テスト.

一括管理は登録済みの全マシンを 1 画面に並べるマシン非依存のページ。押さえるのは次の 3 つ:

- **セクション分け**: frontend 登録の ``machine_type`` で「はんだペースト」「PnP」に分ける。
  種別が分からない機体は「種別不明」に出す（黙って消さない）
- **backend に依存しない描画**: 行の状態は JS が各機体の API から取るので、SSR は
  backend へ問い合わせない（1 台落ちていてもページが開ける）
- **行の配線**: 各行が自分の機体 prefix を持ち、取得 / 解放 / 更新を持つ。塗布実行は
  ペーストの行にだけある

ボタンを押したときの実際の動作は `tests/e2e/test_bulk_browser.py` が実ブラウザで見る。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web.ui.app import create_app
from web.ui.layout import BULK_PATH, bulk_sections
from web.ui.machines import MachineEndpoint
from web.ui.settings import Settings as UiSettings

_PASTE = MachineEndpoint(
    machine_id="paste1", host="127.0.0.1", port=1, machine_type="paste"
)
_PASTE2 = MachineEndpoint(
    machine_id="paste2", host="127.0.0.1", port=2, machine_type="paste"
)
_PNP = MachineEndpoint(machine_id="pnp1", host="127.0.0.1", port=3, machine_type="pnp")
_UNTYPED = MachineEndpoint(machine_id="untyped1", host="127.0.0.1", port=4)


@pytest.fixture
def bulk_client(tmp_path: Path) -> Iterator[TestClient]:
    """全 backend が不通の frontend（4 台登録。port 1〜4 は実 TCP で接続拒否される）."""
    settings = UiSettings(
        machines=(_PASTE, _PNP, _UNTYPED, _PASTE2),
        machines_file=tmp_path / "machines.toml",
        discovery_enabled=False,
        update_state_dir=tmp_path / "selfupdate",
    )
    with TestClient(create_app(settings)) as client:
        yield client


class TestBulkSections:
    """machine_type によるセクション分け."""

    def test_groups_by_machine_type_in_registration_order(self):
        sections = bulk_sections((_PASTE, _PNP, _UNTYPED, _PASTE2))

        assert [
            (section.title, section.machine_type, section.machines)
            for section in sections
        ] == [
            ("はんだペースト", "paste", (_PASTE, _PASTE2)),
            ("PnP", "pnp", (_PNP,)),
            ("種別不明", None, (_UNTYPED,)),
        ]

    def test_paste_and_pnp_sections_exist_even_when_empty(self):
        sections = bulk_sections(())

        assert [(s.machine_type, s.machines) for s in sections] == [
            ("paste", ()),
            ("pnp", ()),
        ]

    def test_unknown_machine_type_goes_to_unknown_section(self):
        odd = MachineEndpoint(
            machine_id="odd", host="127.0.0.1", port=5, machine_type="laser"
        )

        sections = bulk_sections((odd,))

        assert sections[-1].machine_type is None
        assert sections[-1].machines == (odd,)


class TestBulkPage:
    """``/bulk`` の SSR."""

    def test_renders_without_reaching_any_backend(self, bulk_client: TestClient):
        response = bulk_client.get(BULK_PATH)

        assert response.status_code == 200
        for section in ("paste", "pnp", "unknown"):
            assert f'data-testid="bulk-section-{section}"' in response.text

    @pytest.mark.parametrize("machine", [_PASTE, _PNP, _UNTYPED, _PASTE2])
    def test_every_machine_row_has_lease_and_update_controls(
        self, bulk_client: TestClient, machine: MachineEndpoint
    ):
        html = bulk_client.get(BULK_PATH).text

        row = _row(html, machine.machine_id)
        assert f'data-row-base="/m/{machine.machine_id}"' in row
        assert machine.label in row
        assert 'data-testid="bulk-acquire"' in row
        assert 'data-testid="bulk-release"' in row
        assert 'data-testid="bulk-update"' in row

    @pytest.mark.parametrize(
        ("machine", "has_run"),
        [(_PASTE, True), (_PASTE2, True), (_PNP, False), (_UNTYPED, False)],
    )
    def test_only_paste_rows_have_paste_run_button(
        self, bulk_client: TestClient, machine: MachineEndpoint, has_run: bool
    ):
        html = bulk_client.get(BULK_PATH).text

        assert ('data-testid="bulk-paste-run"' in _row(html, machine.machine_id)) is (
            has_run
        )

    def test_pnp_section_says_not_implemented(self, bulk_client: TestClient):
        html = bulk_client.get(BULK_PATH).text

        section = html.split('data-testid="bulk-section-pnp"', 1)[1]
        assert "未実装" in section.split("</section>", 1)[0]

    def test_header_marks_bulk_tab_active(self, bulk_client: TestClient):
        html = bulk_client.get(BULK_PATH).text

        assert f'href="{BULK_PATH}" class="tab active"' in html

    def test_machine_pages_link_to_bulk_tab(self, frontend_client: TestClient):
        html = frontend_client.get("/posctrl").text

        assert f'href="{BULK_PATH}" class="tab"' in html


def _row(html: str, machine_id: str) -> str:
    """``data-testid="bulk-row-<machine_id>"`` の行の HTML 断片."""
    _, _, rest = html.partition(f'data-testid="bulk-row-{machine_id}"')
    assert rest, f"{machine_id} の行がありません"
    return rest.split("</tr>", 1)[0]
