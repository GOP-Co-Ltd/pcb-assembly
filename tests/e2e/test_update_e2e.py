"""WebUI からのソフトウェア更新のフルスタック E2E（実 uvicorn + 実 git）.

`make test-e2e` で実行する。ここでしか見られないのは:

- 実 HTTP 経路（`ASGITransport` ではなく実ソケット）での 202 → ポーリング → 完了
- frontend の proxy を通した backend 側の更新（`/m/{id}/api/update/**`）
- 実ブラウザでの押下 → 進行表示 → 完了表示（`update.js` に相当するテストランナーが
  無いので、DOM が実際に更新されることはここでしか確認できない）

git は実物（tmp 上の bare remote。ネットワークに出ない）、`uv` / `sudo` / `systemctl` は
`UpdateSettings` の絶対パス seam に差した実スタブ実行ファイル。実機の systemd と
sudoers には一切触れない。

**実 systemd の再起動・sudoers の実受理・ネットワーク越しの `uv sync` は Claude が
検証できない範囲**で、ユーザーの実機確認事項（計画書「検証」節）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    E2E_MACHINE_ID as _E2E_MACHINE_ID,
    LiveServer,
    LiveUi,
    acquire_control as _acquire_control,
    make_api_settings as _make_api_settings,
    make_ui_settings as _make_ui_settings,
    start_app as _start_app,
)
from tests.helpers import wait_until
from tests.web.update_support import UpdateSandbox, make_update_sandbox
from web.api.app import create_app
from web.selfupdate.runner import UpdateRunner
from web.ui.app import create_app as create_ui_app
from web.ui.machines import MachineEndpoint

_HTTP_TIMEOUT = 10.0
_UI_TIMEOUT_MS = 15_000


@pytest.fixture
def sandbox(tmp_path: Path) -> UpdateSandbox:
    """被験体の実リポジトリ（origin へ積むのは各テスト。behind 数を固定するため）."""
    return make_update_sandbox(tmp_path / "update")


@pytest.fixture
def update_stack(
    sandbox: UpdateSandbox,
    tmp_path: Path,
    paste_test_board_footprint_root: Path,
) -> Iterator[tuple[LiveServer, LiveUi]]:
    """更新ランナーを差した backend + frontend を実 uvicorn で立てる.

    backend と frontend で **別々の sandbox** を使う（同じ state_dir を共有すると flock
    で片方が必ず断られ、同居機の挙動になってしまう）。ここで見たいのは 「proxy 経由で backend を更新する」経路。
    """
    frontend_sandbox = make_update_sandbox(tmp_path / "frontend-update")
    api_settings = _make_api_settings(tmp_path / "api", hostname=_E2E_MACHINE_ID)
    backend = _start_app(
        create_app(
            api_settings,
            paste_test_board_footprint_root=paste_test_board_footprint_root,
            update_runner=UpdateRunner(sandbox.settings),
        )
    )
    endpoint = MachineEndpoint(
        machine_id=_E2E_MACHINE_ID, host="127.0.0.1", port=backend.port, name="E2E 機"
    )
    frontend = _start_app(
        create_ui_app(
            _make_ui_settings(
                (endpoint,), machines_file=tmp_path / "absent-machines.toml"
            ),
            update_runner=UpdateRunner(frontend_sandbox.settings),
        )
    )
    try:
        yield (
            LiveServer(
                base_url=f"http://127.0.0.1:{backend.port}",
                settings=api_settings,
                port=backend.port,
            ),
            LiveUi(
                origin=f"http://127.0.0.1:{frontend.port}",
                machine_ids=(_E2E_MACHINE_ID,),
            ),
        )
    finally:
        frontend.stop()
        backend.stop()


def _status(base_url: str) -> dict:
    response = httpx.get(f"{base_url}/api/update/status", timeout=_HTTP_TIMEOUT)
    assert response.status_code == 200, response.text
    return response.json()


class TestUpdateOverRealHttp:
    """実ソケット越しの 202 → ポーリング → 完了."""

    def test_update_runs_through_the_frontend_proxy(
        self, update_stack: tuple[LiveServer, LiveUi], sandbox: UpdateSandbox
    ):
        _server, ui = update_stack
        target = sandbox.push()
        assert (
            httpx.post(
                f"{ui.base_url}/api/update/check", timeout=_HTTP_TIMEOUT
            ).status_code
            == 200
        )
        before = _status(ui.base_url)
        assert before["update_available"] is True
        assert before["repository"]["behind"] == 1

        accepted = httpx.post(
            f"{ui.base_url}/api/update/run",
            json={"expected_head": before["repository"]["head"]},
            timeout=_HTTP_TIMEOUT,
        )

        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        wait_until(
            lambda: _status(ui.base_url)["run"]["state"] != "running", timeout=30.0
        )
        final = _status(ui.base_url)
        assert final["run"]["state"] == "restarting", final["run"]
        assert final["run"]["run_id"] == run_id
        assert final["run"]["to_head"] == target
        assert [step["step"] for step in final["run"]["steps"]] == [
            "preflight",
            "fetch",
            "merge",
            "sync",
            "smoke",
        ]
        assert sandbox.head() == target
        wait_until(lambda: sandbox.restarts() != [], timeout=15.0)


class TestUpdatePageInTheBrowser:
    """実ブラウザでの押下 → 進行表示 → 完了表示（update.js の DOM 更新）."""

    def test_backend_update_page_reports_progress_and_completion(
        self,
        update_stack: tuple[LiveServer, LiveUi],
        sandbox: UpdateSandbox,
        browser_page,
    ):
        _server, ui = update_stack
        target = sandbox.push()
        page = browser_page
        page.goto(f"{ui.base_url}/dev/update")
        _acquire_control(page)
        # 更新の有無は fetch しないと見えない（GET status は毎秒叩かれるので fetch しない）
        page.locator("#update-check").click(timeout=_UI_TIMEOUT_MS)
        expect(page.locator("#update-summary")).to_contain_text(
            "1 件の更新", timeout=_UI_TIMEOUT_MS
        )

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#update-run").click(timeout=_UI_TIMEOUT_MS)

        expect(page.locator("#update-run-state")).to_be_visible(timeout=_UI_TIMEOUT_MS)
        expect(page.locator("#update-steps li")).to_have_count(
            5, timeout=_UI_TIMEOUT_MS
        )
        expect(page.locator("#update-state-label")).to_have_text(
            "再起動しています", timeout=_UI_TIMEOUT_MS
        )
        assert sandbox.head() == target
