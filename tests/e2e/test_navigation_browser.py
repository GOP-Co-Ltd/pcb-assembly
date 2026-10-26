"""ページの行き来（着地点・タブ・サイドバー・旧 URL・トップバー・機体の切替）を実ブラウザで確かめる E2E.

利用者の旅程のうち「目的のページへ着く」部分。同居機の `:8080` を開くと自機の塗布ページに着き、
タブとサイドバーで機能を移り、作り直し前のブックマークも新しいページへ飛ぶ。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import attrs
import httpx
import pytest
from playwright.sync_api import expect

from tests.support.browser import (
    E2E_MACHINE_ID as _E2E_MACHINE_ID,
    LiveServer,
    LiveUi,
    start_app as _start_app,
)
from tests.support.web import make_ui_settings as _make_ui_settings
from web.ui.app import create_app as create_ui_app
from web.ui.machines import MachineEndpoint, MachineRegistry

_HTTP_TIMEOUT = 10.0

# 一覧に出るだけで誰も開かない登録（ドロップダウンを 2 件にして選択状態を見る）
_GHOST_MACHINE_ID = "ghost"


@pytest.fixture
def selector_ui(
    live_server: LiveServer, tmp_path: Path
) -> Iterator[tuple[str, MachineRegistry]]:
    """`live_server` と到達不能な 1 台を登録した実 frontend（origin と registry）.

    registry を返すのは、mDNS 探索の結果が届いたときに相当する変化
    （`set_discovered`）をテスト側から起こして、ドロップダウンの組み替えを
    見られるようにするため（実 zeroconf は使わない = 実 LAN に触らない）。
    """
    endpoints = (
        MachineEndpoint(machine_id=_GHOST_MACHINE_ID, host="127.0.0.1", port=1),
        MachineEndpoint(
            machine_id=_E2E_MACHINE_ID, host="127.0.0.1", port=live_server.port
        ),
    )
    app = create_ui_app(
        _make_ui_settings(endpoints, machines_file=tmp_path / "absent-machines.toml")
    )
    running = _start_app(app)
    try:
        yield f"http://127.0.0.1:{running.port}", app.state.registry
    finally:
        running.stop()


def _machine_labels(origin: str, *, current: str) -> list[str]:
    """`GET /api/machines` が返す表示名（サーバが組んだ文字列をそのまま期待値にする）."""
    response = httpx.get(
        f"{origin}/api/machines", params={"current": current}, timeout=_HTTP_TIMEOUT
    )
    assert response.status_code == 200, response.text
    return [machine["label"] for machine in response.json()["machines"]]


@pytest.fixture
def own_ui(live_server: LiveServer, tmp_path: Path) -> Iterator[LiveUi]:
    """同居機の frontend（自身のホスト名が `live_server` の machine_id と同じ）."""
    endpoint = MachineEndpoint(
        machine_id=_E2E_MACHINE_ID, host="127.0.0.1", port=live_server.port
    )
    settings = attrs.evolve(
        _make_ui_settings((endpoint,), machines_file=tmp_path / "absent-machines.toml"),
        hostname=_E2E_MACHINE_ID,
    )
    running = _start_app(create_ui_app(settings))
    try:
        yield LiveUi(
            origin=f"http://127.0.0.1:{running.port}", machine_ids=(_E2E_MACHINE_ID,)
        )
    finally:
        running.stop()


class TestLanding:
    """毎日使う操作に、クリック無しで着く."""

    def test_this_host_opens_its_paste_page(self, own_ui: LiveUi, browser_page):
        browser_page.goto(f"{own_ui.origin}/")

        expect(browser_page).to_have_url(f"{own_ui.base_url}/paste_solder")
        tabs = browser_page.get_by_test_id("top-tabs")
        expect(tabs.get_by_role("link", name="塗布", exact=True)).to_have_attribute(
            "aria-current", "true"
        )

    def test_old_bookmark_opens_the_new_page(self, live_ui: LiveUi, browser_page):
        browser_page.goto(f"{live_ui.base_url}/pasting/nozzle_cap")

        expect(browser_page).to_have_url(f"{live_ui.base_url}/nozzle_cap")
        expect(browser_page.get_by_test_id("sidebar")).to_be_visible()


class TestFeatureNavigationBrowser:
    def test_tab_opens_its_first_feature_and_the_sidebar_marks_the_current_page(
        self, live_ui: LiveUi, browser_page
    ):
        browser_page.set_viewport_size({"width": 1440, "height": 900})
        browser_page.goto(f"{live_ui.base_url}/paste_solder")
        tabs = browser_page.get_by_test_id("top-tabs")
        tabs.get_by_role("link", name="校正", exact=True).click()

        # 校正タブの入口は立ち上げチェックリスト
        expect(browser_page).to_have_url(f"{live_ui.base_url}/commissioning")
        navigation = browser_page.get_by_role("navigation", name="校正の機能")
        expect(
            navigation.get_by_role("heading", name="ノズル・プローブ")
        ).to_be_visible()
        navigation.get_by_role("link", name="ノズル位置の設定", exact=False).click()

        expect(browser_page).to_have_url(f"{live_ui.base_url}/nozzle_cap")
        navigation = browser_page.get_by_role("navigation", name="校正の機能")
        active = navigation.get_by_role("link", name="ノズル位置の設定", exact=False)
        expect(active).to_have_attribute("aria-current", "page")
        # 長い名前も省略せず、リンクの枠内で最後まで読める。
        assert active.evaluate("el => el.scrollWidth <= el.clientWidth")

    def test_mobile_menu_is_keyboard_accessible_and_survives_resize(
        self, live_ui: LiveUi, browser_page
    ):
        browser_page.set_viewport_size({"width": 390, "height": 844})
        browser_page.goto(f"{live_ui.base_url}/camera_preview")
        menu = browser_page.get_by_test_id("feature-menu")
        navigation = browser_page.get_by_role("navigation", name="校正の機能")
        expect(navigation).to_be_hidden()
        summary = menu.locator("summary")
        expect(summary).to_contain_text("カメラプレビュー")
        summary.focus()
        summary.press("Enter")
        expect(navigation).to_be_visible()
        navigation.get_by_role("link", name="銅箔検出調整", exact=True).click()
        expect(browser_page).to_have_url(f"{live_ui.base_url}/copper_detection")
        expect(navigation).to_be_hidden()
        expect(summary).to_contain_text("銅箔検出調整")

        browser_page.set_viewport_size({"width": 1440, "height": 900})
        expect(navigation).to_be_visible()
        browser_page.set_viewport_size({"width": 390, "height": 844})
        expect(navigation).to_be_hidden()
        assert browser_page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )


class TestMobileTopbar:
    @pytest.mark.parametrize("width", [320, 390, 768])
    def test_menu_supports_keyboard_control_and_desktop_resize(
        self, live_ui: LiveUi, browser_page, width: int
    ):
        browser_page.set_viewport_size({"width": width, "height": 844})
        browser_page.goto(f"{live_ui.base_url}/camera_preview")
        menu = browser_page.get_by_test_id("topbar-menu")
        summary = menu.locator("summary")
        acquire = browser_page.get_by_test_id("control-acquire")
        expect(acquire).to_be_hidden()
        expect(browser_page.get_by_test_id("machine-select")).to_be_visible()
        expect(browser_page.get_by_test_id("estop")).to_be_visible()

        summary.focus()
        summary.press("Enter")
        expect(acquire).to_be_visible()
        browser_page.get_by_test_id("control-name").fill("モバイル操作者")
        acquire.click()
        expect(browser_page.locator("body")).to_have_attribute("data-control", "held")
        browser_page.get_by_test_id("control-release").click()
        expect(browser_page.locator("body")).to_have_attribute("data-control", "free")
        assert browser_page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )

        summary.focus()
        summary.press("Enter")
        expect(acquire).to_be_hidden()
        browser_page.set_viewport_size({"width": 1440, "height": 900})
        expect(summary).to_be_hidden()
        expect(acquire).to_be_visible()
        browser_page.set_viewport_size({"width": width, "height": 844})
        expect(acquire).to_be_hidden()


class TestMachineSelectorRefresh:
    """マシン選択ドロップダウンをサーバの `/api/machines` で組み替える.

    `machine_selector.js` の catch は全例外を飲むので、取得・`data-*` 参照・option の
    組み替えのどれが壊れても SSR 済みの option がそのまま残り、無音で劣化する
    （mDNS で見つかったマシンが一覧に出てこない）。実ブラウザで
    「サーバの label で組み替わる」「current のマシンが選択される」を通しで見る。
    """

    def test_machine_list_picker_stays_unselected_and_opens_a_machine_with_keyboard(
        self, selector_ui: tuple[str, MachineRegistry], browser_page
    ):
        origin, _ = selector_ui
        with browser_page.expect_response(
            lambda response: "/api/machines?" in response.url
        ):
            browser_page.goto(f"{origin}/machines")
        select = browser_page.get_by_test_id("machine-select")
        expect(select).to_have_value("")
        expect(browser_page.get_by_test_id("estop")).to_have_count(0)

        with browser_page.expect_response(
            lambda response: "/api/machines?" in response.url
        ):
            browser_page.evaluate(
                "document.dispatchEvent(new Event('visibilitychange'))"
            )
        expect(select).to_have_value("")
        select.focus()
        select.press("ArrowDown")

        # 機体の着地点（/m/{machine_id}）を開く。着地点を申告する backend に届かないので 503 のページになる
        expect(browser_page).to_have_url(f"{origin}/m/{_GHOST_MACHINE_ID}")
        expect(
            browser_page.get_by_role("heading", name="マシンに接続できません")
        ).to_be_visible()
        # 対象が決まった画面は、接続エラー中も緊急停止の入口を残す。
        expect(browser_page.get_by_test_id("estop")).to_be_visible()

    def test_unregistered_frontend_has_no_unbound_machine_commands(
        self, tmp_path: Path, browser_page
    ):
        app = create_ui_app(
            _make_ui_settings((), machines_file=tmp_path / "absent.toml")
        )
        running = _start_app(app)
        errors: list[str] = []
        browser_page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            browser_page.goto(f"http://127.0.0.1:{running.port}/")
            for control in ("control-lease", "pcb-chip", "firmware-restart", "estop"):
                expect(browser_page.get_by_test_id(control)).to_have_count(0)
            expect(browser_page.get_by_test_id("update-badge")).to_have_count(1)
            assert errors == []
        finally:
            running.stop()

    def test_options_are_rebuilt_from_the_server_labels(
        self,
        live_server: LiveServer,
        selector_ui: tuple[str, MachineRegistry],
        browser_page,
    ):
        origin, registry = selector_ui

        with browser_page.expect_request(
            lambda request: "/api/machines" in request.url, timeout=10_000
        ) as fetched:
            browser_page.goto(
                f"{origin}/m/{_E2E_MACHINE_ID}/klipper_status",
                wait_until="domcontentloaded",
            )

        # frontend 自身のエンドポイントを叩く（machine prefix を付けると backend へ
        # 中継されて 404）。current の判定はサーバ側なのでクエリに載る
        assert fetched.value.url == f"{origin}/api/machines?current={_E2E_MACHINE_ID}"

        options = browser_page.get_by_test_id("machine-select").locator(
            'option:not([value=""])'
        )
        expect(options).to_have_text(_machine_labels(origin, current=_E2E_MACHINE_ID))
        # 表示中のマシンが選択されている（登録順では 2 番目なので既定選択とは異なる）
        expect(browser_page.get_by_test_id("machine-select")).to_have_value(
            f"/m/{_E2E_MACHINE_ID}/klipper_status"
        )

        # mDNS で新しい機体が見つかったときに相当する変化を起こす。
        # label は hostname: host なので、同じ機体の自由入力名を変えても表示は変わらない。
        registry.set_discovered(
            (
                MachineEndpoint(
                    machine_id="discovered-machine",
                    host="127.0.0.1",
                    port=live_server.port,
                    source="mdns",
                ),
            )
        )
        browser_page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

        expect(options).to_have_text(_machine_labels(origin, current=_E2E_MACHINE_ID))
        expect(options).to_have_count(3)
        expect(options.nth(2)).to_contain_text("discovered-machine")
        expect(browser_page.get_by_test_id("machine-select")).to_have_value(
            f"/m/{_E2E_MACHINE_ID}/klipper_status"
        )

        registry.set_discovered(())
        browser_page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(options).to_have_text(_machine_labels(origin, current=_E2E_MACHINE_ID))
        expect(options).to_have_count(2)
        expect(browser_page.get_by_test_id("machine-select")).to_have_value(
            f"/m/{_E2E_MACHINE_ID}/klipper_status"
        )
