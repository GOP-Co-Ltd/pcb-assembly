"""WebUI フルスタック E2E の fixture（実 uvicorn の backend と frontend、実 Chromium）.

実 uvicorn を 127.0.0.1 のエフェメラルポートに起動し、実 HTTP / WebSocket / MJPEG 経路を fake カメラ + テスト用 config で検証する。
サーバーの生存期間を fixture（= pytest プロセス）の中に閉じ込めるので、`make test-e2e` の中で起動から停止までが完結する。
実機の `config/` を汚さないよう、tests/data/config を tmp_path に複製して使う。

サーバーは 2 種類ある:

- `live_server` — backend WebAPI（`web.api`）。JSON API の直叩きに使う
- `live_ui` — UI frontend（`web.ui`）。**ページ・MJPEG・WS はここを通す**

ページ取得とブラウザ操作を frontend 経由に寄せることで、リバースプロキシ経路
（`/m/{machine_id}/api/**`）が既存 E2E 全体で常時検証される。API の直叩きを
`live_server` のまま残すのは、backend 直と proxy 経由を意図的に分けて
「どちら側の回帰か」を切り分けられるようにするため。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pcbasm.hal.fake import FakeAudioPlayer
from tests.support.browser import (
    E2E_MACHINE_ID,
    LiveServer,
    LiveUi,
    register_completion_notice_jobs,
    start_app,
)
from tests.support.job_demo import register_job_demo
from tests.support.web import devices_with_audio, make_api_settings, make_ui_settings
from web.api.app import create_app
from web.api.env import Env
from web.ui.app import create_app as create_ui_app
from web.ui.machines import MachineEndpoint

# 実ブラウザを使う fixture。これを要求するテストへ browser マーカーを付ける
_BROWSER_FIXTURES = frozenset({"browser_page", "browser_pages"})


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Tests/e2e 配下の全テストへ自動で e2e / browser マーカーを付与する.

    個々のテストに付け忘れても `-m e2e` / `-m "not e2e"` の対象になるようにする。
    実ブラウザ（`browser_page` / `browser_pages`）を使うテストには browser も付け、
    `-m "e2e and not browser"` での切り分けを可能にする。
    """
    e2e_dir = Path(__file__).parent
    for item in items:
        if item.path.is_relative_to(e2e_dir):
            item.add_marker(pytest.mark.e2e)
            if _BROWSER_FIXTURES & set(getattr(item, "fixturenames", ())):
                item.add_marker(pytest.mark.browser)


@pytest.fixture(autouse=True)
def moonraker_is_down(unreachable_klipper: None) -> None:
    """E2E のサーバーはこのプロセス内で動くので、Moonraker 不通の機体として振る舞わせる."""


@pytest.fixture
def e2e_settings(tmp_path: Path) -> Env:
    """Fake カメラ + テスト用 config + 隔離 data_dir の E2E 用 backend Env."""
    return make_api_settings(tmp_path / "api", hostname=E2E_MACHINE_ID)


@pytest.fixture
def live_server(
    e2e_settings: Env,
    paste_test_board_footprint_root: Path,
) -> Iterator[LiveServer]:
    """実 uvicorn の backend WebAPI を起動し、停止まで面倒を見る.

    通知音は `FakeAudioPlayer` を注入する（既定の `AlsaAudioPlayer` だとジョブの
    完了・応答待ちが実 `aplay` を起動して実スピーカーが鳴る）。
    ジョブ機構を通す題材として検証用の ``job_demo`` も登録する。
    """
    app = create_app(
        e2e_settings,
        devices=devices_with_audio(e2e_settings, FakeAudioPlayer()),
        paste_test_board_footprint_root=paste_test_board_footprint_root,
    )
    register_job_demo(app.state.backend.catalog)
    register_completion_notice_jobs(app.state.backend.catalog)
    running = start_app(app)
    try:
        yield LiveServer(
            base_url=f"http://127.0.0.1:{running.port}",
            settings=e2e_settings,
            port=running.port,
        )
    finally:
        running.stop()


@pytest.fixture
def live_ui(live_server: LiveServer, tmp_path: Path) -> Iterator[LiveUi]:
    """`live_server` を 1 台だけ静的登録した実 frontend.

    ページ・MJPEG・WS をここへ通すことで、リバースプロキシ経路が既存 E2E 全体で 常時検証される。
    """
    endpoint = MachineEndpoint(
        machine_id=E2E_MACHINE_ID,
        host="127.0.0.1",
        port=live_server.port,
    )
    app = create_ui_app(
        make_ui_settings((endpoint,), machines_file=tmp_path / "absent-machines.toml")
    )
    running = start_app(app)
    try:
        yield LiveUi(
            origin=f"http://127.0.0.1:{running.port}",
            machine_ids=(E2E_MACHINE_ID,),
        )
    finally:
        running.stop()


@pytest.fixture(scope="session")
def _browser():
    """Session 共有の実 Chromium（起動はセッションで 1 回）.

    pytest-playwright が未導入の環境では collection を壊さず skip する。Chromium
    はこの環境にある system binary を優先し、無ければ Playwright 既定に任せる。 テスト間の分離は
    browser_page 側の browser context で担保する。
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {"headless": True}
        if Path("/usr/bin/chromium").exists():
            launch_kwargs["executable_path"] = "/usr/bin/chromium"
        browser = playwright.chromium.launch(**launch_kwargs)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def browser_pages(_browser):
    """独立した browser context の page を必要な数だけ作るファクトリ.

    `browser_page` の実装を集約する（現状の利用は 1 枚のみ。複数ページを開く検証は
    MR6 以降で使う想定）。作った context は teardown でまとめて閉じる。
    """
    contexts = []

    def new_page():
        context = _browser.new_context()
        contexts.append(context)
        return context.new_page()

    try:
        yield new_page
    finally:
        for context in contexts:
            context.close()


@pytest.fixture
def browser_page(browser_pages):
    """Playwright sync API の実 Chromium page（1 枚）.

    テストごとに新しい browser context（cookie / localStorage / viewport が
    独立）を作り、teardown で context ごと閉じる。
    """
    return browser_pages()
