"""WebUI フルスタック E2E 用の live uvicorn サーバー fixture.

実 uvicorn を 127.0.0.1 のエフェメラルポートに起動し、実 HTTP / WebSocket / MJPEG 経路を fake
カメラ + test-fixture マシンで検証する。サーバーの生存期間を fixture（= pytest
プロセス）内に閉じ込めるため常駐サーバーを別管理する必要がなく、 `make test-e2e` という有限コマンドの中で起動 → 検証 →
停止が完結する（常駐サーバーは Bash のタイムアウトやプロセス後始末で kill されがちで、E2E デバッグの障害になる）。

実機設定（configs/ 直下の kurousagi 等）を一切汚さないよう、test-fixture を tmp_path に
複製して使う（[[feedback-webui-claude-self-e2e]] の方針）。
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import attrs
import pytest
import uvicorn

from tests.webui.conftest import FAKE_CAMERA_IMAGE, TEST_FIXTURE_DIR
from webui.app import create_app
from webui.settings import Settings

_STARTUP_TIMEOUT = 10.0


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Tests/e2e 配下の全テストへ自動で e2e / browser マーカーを付与する.

    個々のテストに付け忘れても `-m e2e` / `-m "not e2e"` の対象になるようにする。
    実ブラウザ（browser_page）を使うテストには browser も付け、 `-m "e2e and not browser"`
    での切り分けを可能にする。
    """
    e2e_dir = Path(__file__).parent
    for item in items:
        if item.path.is_relative_to(e2e_dir):
            item.add_marker(pytest.mark.e2e)
            if "browser_page" in getattr(item, "fixturenames", ()):
                item.add_marker(pytest.mark.browser)


@attrs.frozen
class LiveServer:
    """起動済み実サーバーのベース URL と注入 Settings."""

    base_url: str
    settings: Settings

    @property
    def ws_url(self) -> str:
        """WebSocket 用ベース URL（http -> ws）."""
        return "ws://" + self.base_url.removeprefix("http://")


@pytest.fixture
def e2e_settings(tmp_path: Path) -> Settings:
    """Fake カメラ + test-fixture マシン + 隔離 data_dir の E2E 用 Settings.

    test-fixture を tmp_path 内に "kurousagi"（既定選択）と "test-fixture" の 2
    マシンとして複製する。どちらも Klipper port 7126（非リッスン）なので、 誤って実機 Moonraker に接続しない。
    """
    configs_root = tmp_path / "configs"
    shutil.copytree(TEST_FIXTURE_DIR, configs_root / "kurousagi")
    shutil.copytree(TEST_FIXTURE_DIR, configs_root / "test-fixture")
    pcb_root = tmp_path / "pcb"
    pcb_root.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Settings(
        configs_root=configs_root,
        data_dir=data_dir,
        pcb_browse_root=pcb_root,
        pcb_browse_start=pcb_root,
        pcb_upload_dir=pcb_root / "uploads",
        mainsail_url="http://mainsail.invalid",
        default_machine="kurousagi",
        fake_camera=True,
        fake_camera_image=FAKE_CAMERA_IMAGE,
    )


@pytest.fixture
def live_server(e2e_settings: Settings) -> Iterator[LiveServer]:
    """実 uvicorn を daemon スレッドで起動し、停止まで面倒を見る.

    port=0 でエフェメラルポートを OS に割り当てさせ、起動後に実ポートを取得する。
    """
    config = uvicorn.Config(
        create_app(e2e_settings),
        host="127.0.0.1",
        port=0,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn が時間内に起動しなかった")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]

    try:
        yield LiveServer(base_url=f"http://127.0.0.1:{port}", settings=e2e_settings)
    finally:
        server.should_exit = True
        thread.join(timeout=_STARTUP_TIMEOUT)


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
def browser_page(_browser):
    """Playwright sync API の実 Chromium page.

    テストごとに新しい browser context（cookie / localStorage / viewport が
    独立）を作り、teardown で context ごと閉じる。
    """
    context = _browser.new_context()
    try:
        yield context.new_page()
    finally:
        context.close()
