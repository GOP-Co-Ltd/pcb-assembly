"""パッド階層表の再描画をまたいでキーボード操作を続けられる。"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    E2E_MACHINE_ID,
    LiveServer,
    LiveUi,
    acquire_control,
    get_pad_config,
    make_ui_settings,
    select_led_blinker,
    start_app,
)
from tests.e2e.network import DelayedHttp
from web.ui.app import create_app
from web.ui.machines import MachineEndpoint


@pytest.fixture
def delayed_save_ui(
    live_server: LiveServer, tmp_path: Path
) -> Iterator[tuple[LiveUi, DelayedHttp]]:
    endpoint = MachineEndpoint(
        machine_id=E2E_MACHINE_ID, host="127.0.0.1", port=live_server.port
    )
    transport = DelayedHttp(
        create_app(
            make_ui_settings((endpoint,), machines_file=tmp_path / "absent.toml")
        ),
        method="PATCH",
        path_suffixes=("/api/pasting/pad-config/node",),
        response=True,
    )
    server = start_app(transport)
    try:
        yield (
            LiveUi(
                origin=f"http://127.0.0.1:{server.port}", machine_ids=(E2E_MACHINE_ID,)
            ),
            transport,
        )
    finally:
        transport.resume()
        server.stop()


def _open_editor(live_server: LiveServer, live_ui: LiveUi, page):
    select_led_blinker(live_server)
    page.goto(f"{live_ui.base_url}/pasting/paste_solder", wait_until="domcontentloaded")
    acquire_control(page)
    root = page.locator('tr[data-node-id="L0"]')
    expect(root).to_be_visible()
    return root


class TestPadEditorKeyboard:
    def test_an_unedited_focused_number_follows_server_updates(
        self, live_server: LiveServer, delayed_save_ui, browser_page
    ):
        ui, transport = delayed_save_ui
        page = browser_page
        root = _open_editor(live_server, ui, page)
        field = root.locator('input[data-field="ul_per_mm2"]')
        child = page.locator(
            'tr[data-node-id]:not([data-node-id="L0"]) input[data-field="ul_per_mm2"]'
        ).first
        child.fill("0.23")
        field.focus()
        assert transport.wait_received()

        response = page.request.patch(
            f"{ui.base_url}/api/pasting/pad-config/node",
            data={"node": "L0", "values": {"ul_per_mm2": 0.31}},
        )
        assert response.status == 200
        with page.expect_response("**/api/pasting/pad-config"):
            transport.resume()
        expect(field).to_have_value("0.31")
        expect(field).to_be_focused()

    @pytest.mark.parametrize(
        ("field_name", "draft"),
        [("ul_per_mm2", "0.23"), ("paste_height", "0.23"), ("paste_height", "")],
    )
    def test_another_save_preserves_the_number_being_edited(
        self,
        live_server: LiveServer,
        delayed_save_ui,
        browser_page,
        field_name: str,
        draft: str,
    ):
        ui, transport = delayed_save_ui
        page = browser_page
        root = _open_editor(live_server, ui, page)
        first = root.locator('input[data-field="ul_per_mm2"]')
        first.fill("0.15")

        row = page.locator('tr[data-node-id]:not([data-node-id="L0"])').first
        field = row.locator(f'input[data-field="{field_name}"]')
        if field_name == "paste_height":
            row.locator('select[data-field="paste_height"]').select_option("manual")
        field.focus()
        assert transport.wait_received()
        field.fill(draft)

        with page.expect_response("**/api/pasting/pad-config"):
            transport.resume()
        expect(
            root.locator(
                '[data-testid="pad-own-override-marker"][data-field="ul_per_mm2"]'
            )
        ).to_be_attached()
        # 値が後から偶然保存されて戻るまで待たず、再描画直後の入力を確かめる。
        assert field.input_value() == draft
        expect(field).to_be_visible()
        expect(field).to_be_focused()
        field.fill("0.23")
        field.press("Enter")
        expect(
            row.locator(
                f'[data-testid="pad-own-override-marker"][data-field="{field_name}"]'
            )
        ).to_be_attached()

        page.reload()
        expect(field).to_have_value("0.23")
        if field_name == "paste_height":
            expect(row.locator('select[data-field="paste_height"]')).to_have_value(
                "manual"
            )

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
