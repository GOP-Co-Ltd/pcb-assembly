"""一括管理ページ（``/bulk``）の行操作を実ブラウザで確かめる E2E.

SSR テスト（`tests/web/ui/test_bulk.py`）は行の配線までしか見られない。ここでは
行のボタンが ``/m/{machine_id}/api/**`` 経由でその機体の backend に効くことを見る。

- 行で取った操作権は、その機体のページでも自分のものになる（同じセッション cookie）
- 塗布実行は backend のジョブ開始 API に届く（PCB 未選択なので backend が 400 で拒み、
  その理由が画面に出る。装置は動かない）
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    E2E_MACHINE_ID,
    LiveServer,
    LiveUi,
    make_ui_settings,
    start_app,
)
from web.ui.app import create_app
from web.ui.machines import MachineEndpoint


@pytest.fixture
def paste_ui(live_server: LiveServer, tmp_path: Path) -> Iterator[LiveUi]:
    """`live_server` をペースト機として 1 台登録した実 frontend."""
    endpoint = MachineEndpoint(
        machine_id=E2E_MACHINE_ID,
        host="127.0.0.1",
        port=live_server.port,
        machine_type="paste",
    )
    running = start_app(
        create_app(
            make_ui_settings(
                (endpoint,), machines_file=tmp_path / "absent-machines.toml"
            )
        )
    )
    try:
        yield LiveUi(
            origin=f"http://127.0.0.1:{running.port}", machine_ids=(E2E_MACHINE_ID,)
        )
    finally:
        running.stop()


class TestBulkRow:
    def test_acquire_in_row_is_shared_with_machine_page_and_release(
        self, paste_ui: LiveUi, browser_page
    ):
        browser_page.goto(f"{paste_ui.origin}/bulk")
        row = browser_page.get_by_test_id(f"bulk-row-{E2E_MACHINE_ID}")
        holder = row.get_by_test_id("bulk-holder")
        expect(holder).to_have_text("空き")

        row.get_by_test_id("bulk-acquire").click()
        expect(holder).to_have_text("あなた")
        expect(row.get_by_test_id("bulk-acquire")).to_be_hidden()

        browser_page.goto(f"{paste_ui.base_url}/posctrl")
        expect(browser_page.locator("body")).to_have_attribute("data-control", "held")

        browser_page.goto(f"{paste_ui.origin}/bulk")
        row.get_by_test_id("bulk-release").click()
        expect(holder).to_have_text("空き")
        expect(row.get_by_test_id("bulk-release")).to_be_hidden()

    def test_paste_run_reaches_backend_job_api(self, paste_ui: LiveUi, browser_page):
        browser_page.goto(f"{paste_ui.origin}/bulk")
        row = browser_page.get_by_test_id(f"bulk-row-{E2E_MACHINE_ID}")
        run = row.get_by_test_id("bulk-paste-run")
        expect(run).to_be_disabled()

        row.get_by_test_id("bulk-acquire").click()
        expect(run).to_be_enabled()
        browser_page.once("dialog", lambda dialog: dialog.accept())
        run.click()

        expect(browser_page.locator(".toast.error")).to_contain_text(
            "PCB ファイルの選択が必要"
        )
