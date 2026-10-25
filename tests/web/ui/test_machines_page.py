"""機体一覧（``/machines``）の仕様テスト.

機体一覧は登録済みの全マシンを 1 画面に登録順で並べるマシン非依存のページ。押さえるのは次の 2 つ:

- **backend に依存しない描画**: 行の状態は JS が各機体の API から取るので、SSR は
  backend へ問い合わせない（1 台落ちていてもページが開ける）
- **行の配線**: 各行が自分の機体 prefix を持ち、取得 / 解放 / 塗布実行と、更新ページへのリンクを持つ

ボタンを押したときの実際の動作は `tests/e2e/test_machines_browser.py` が実ブラウザで見る。
"""

from collections.abc import Iterator
from pathlib import Path

import attrs
import pytest
from fastapi.testclient import TestClient

from web.ui.app import create_app
from web.ui.machines import MachineEndpoint
from web.ui.pages import MACHINES_PATH
from web.ui.settings import Settings as UiSettings

_FIRST = MachineEndpoint(machine_id="paste1", host="127.0.0.1", port=1)
_SECOND = MachineEndpoint(machine_id="paste2", host="127.0.0.1", port=2)


@pytest.fixture
def machines_client(tmp_path: Path) -> Iterator[TestClient]:
    """全 backend が不通の frontend（2 台登録。port 1〜2 は実 TCP で接続拒否される）."""
    settings = UiSettings(
        machines=(_FIRST, _SECOND),
        machines_file=tmp_path / "machines.toml",
        discovery_enabled=False,
        update_state_dir=tmp_path / "selfupdate",
    )
    with TestClient(create_app(settings)) as client:
        yield client


class TestMachinesPage:
    """``/machines`` の SSR."""

    def test_renders_rows_in_registration_order_without_reaching_any_backend(
        self, machines_client: TestClient
    ):
        response = machines_client.get(MACHINES_PATH)

        assert response.status_code == 200
        text = response.text
        assert text.index('data-testid="machines-row-paste1"') < text.index(
            'data-testid="machines-row-paste2"'
        )

    def test_says_so_when_no_machine_is_registered(self, tmp_path: Path):
        settings = UiSettings(
            machines=(),
            machines_file=tmp_path / "machines.toml",
            discovery_enabled=False,
            update_state_dir=tmp_path / "selfupdate",
        )
        with TestClient(create_app(settings)) as client:
            text = client.get(MACHINES_PATH).text

        assert "登録されている機体はありません" in text
        # 登録の仕方を案内する
        assert "machines.toml" in text
        assert "machines-table" not in text

    @pytest.mark.parametrize("machine", [_FIRST, _SECOND])
    def test_every_machine_row_has_lease_and_paste_controls_and_links_the_update_page(
        self, machines_client: TestClient, machine: MachineEndpoint
    ):
        html = machines_client.get(MACHINES_PATH).text

        row = _row(html, machine.machine_id)
        assert f'data-row-base="/m/{machine.machine_id}"' in row
        assert machine.label in row
        assert 'data-testid="machines-acquire"' in row
        assert 'data-testid="machines-release"' in row
        assert 'data-testid="machines-paste-run"' in row
        # 更新の操作は更新ページにまとめ、行には状態とリンクだけを置く
        assert 'data-testid="machines-update-summary"' in row
        assert 'href="/update"' in row
        assert f'href="/m/{machine.machine_id}/paste_solder"' in row

    def test_update_only_row_links_to_the_update_page_instead_of_row_controls(
        self, tmp_path: Path
    ):
        """版のずれた機体は取得・塗布実行を中継できないので、更新ページへ案内する."""
        old = attrs.evolve(_FIRST, update_only=True)
        settings = UiSettings(
            machines=(old,),
            machines_file=tmp_path / "machines.toml",
            discovery_enabled=False,
            update_state_dir=tmp_path / "selfupdate",
        )
        with TestClient(create_app(settings)) as client:
            row = _row(client.get(MACHINES_PATH).text, old.machine_id)

        assert old.label in row
        assert 'href="/update"' in row
        assert "data-row-base" not in row
        assert 'data-testid="machines-acquire"' not in row

    def test_header_marks_the_machines_link_active(self, machines_client: TestClient):
        html = machines_client.get(MACHINES_PATH).text

        assert f'href="{MACHINES_PATH}" class="tab active"' in html

    def test_machine_pages_link_to_the_machines_page(self, frontend_client: TestClient):
        html = frontend_client.get("/klipper_status").text

        assert f'href="{MACHINES_PATH}" class="tab"' in html


def _row(html: str, machine_id: str) -> str:
    """``data-testid="machines-row-<machine_id>"`` の行の HTML 断片."""
    _, _, rest = html.partition(f'data-testid="machines-row-{machine_id}"')
    assert rest, f"{machine_id} の行がありません"
    return rest.split("</tr>", 1)[0]
