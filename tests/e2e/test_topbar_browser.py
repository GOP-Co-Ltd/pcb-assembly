"""狭い画面でも機体選択と緊急停止へアクセスでき、共通操作が使える。"""

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import LiveUi


class TestMobileTopbar:
    @pytest.mark.parametrize("width", [320, 390, 768])
    def test_menu_supports_keyboard_control_and_desktop_resize(
        self, live_ui: LiveUi, browser_page, width: int
    ):
        browser_page.set_viewport_size({"width": width, "height": 844})
        browser_page.goto(f"{live_ui.base_url}/posctrl/camera_preview")
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
