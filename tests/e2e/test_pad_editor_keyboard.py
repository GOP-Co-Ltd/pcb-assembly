"""パッド階層表の再描画をまたいでキーボード操作を続けられる。"""

import re

from playwright.sync_api import expect

from tests.e2e.conftest import (
    LiveServer,
    LiveUi,
    acquire_control,
    get_pad_config,
    select_led_blinker,
)


def _open_editor(live_server: LiveServer, live_ui: LiveUi, page):
    select_led_blinker(live_server)
    page.goto(f"{live_ui.base_url}/pasting/paste_solder", wait_until="domcontentloaded")
    acquire_control(page)
    root = page.locator('tr[data-node-id="L0"]')
    expect(root).to_be_visible()
    return root


class TestPadEditorKeyboard:
    def test_toggle_keeps_focus_and_reports_expansion(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        root = _open_editor(live_server, live_ui, browser_page)
        toggle = root.get_by_test_id("pad-tree-toggle")
        toggle.focus()
        toggle.press("Enter")
        expect(browser_page.get_by_test_id("pad-tree-row")).to_have_count(1)
        expect(toggle).to_be_focused()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_have_accessible_name(
            get_pad_config(live_server)["tree"]["label"]
        )

        browser_page.keyboard.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(toggle).to_be_focused()
        browser_page.keyboard.press("Tab")
        expect(root.get_by_test_id("pad-enabled-checkbox")).to_be_focused()

    def test_saved_value_and_cleared_override_keep_the_editing_position(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        root = _open_editor(live_server, live_ui, browser_page)
        field = root.locator('input[data-field="ul_per_mm2"]')
        field.fill("0.15")
        field.press("Enter")
        marker = root.locator(
            '[data-testid="pad-own-override-marker"][data-field="ul_per_mm2"]'
        )
        expect(marker).to_be_attached()
        expect(field).to_be_focused()
        expect(field).to_have_value("0.15")
        expect(field).to_have_accessible_name(
            re.compile(re.escape(get_pad_config(live_server)["tree"]["label"]))
        )

        clear = root.locator(
            '[data-testid="pad-clear-override"][data-field="ul_per_mm2"]'
        )
        clear.focus()
        clear.press("Enter")
        expect(marker).not_to_be_attached()
        expect(field).to_be_focused()
        expect(field).to_have_value("")

    def test_enabled_checkbox_keeps_focus_across_server_updates(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        root = _open_editor(live_server, live_ui, browser_page)
        checkbox = root.get_by_test_id("pad-enabled-checkbox")
        checkbox.focus()
        checkbox.press("Space")
        expect(root).to_have_class(re.compile("pad-row-disabled"))
        expect(checkbox).to_be_focused()
        label = get_pad_config(live_server)["tree"]["label"]
        expect(checkbox).to_have_accessible_name(f"{label} 有効")

        browser_page.keyboard.press("Space")
        expect(root).not_to_have_class(re.compile("pad-row-disabled"))
        expect(checkbox).to_be_checked()
        expect(checkbox).to_be_focused()
