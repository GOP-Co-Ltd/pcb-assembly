"""機能の入口とレスポンシブな一覧を、実ページのリンク・キーボード操作で検証する。"""

from playwright.sync_api import expect

from tests.e2e.conftest import LiveUi


class TestFeatureNavigationBrowser:
    def test_overview_opens_a_feature_and_marks_the_current_location(
        self, live_ui: LiveUi, browser_page
    ):
        browser_page.set_viewport_size({"width": 1440, "height": 900})
        browser_page.goto(f"{live_ui.base_url}/pasting")
        overview = browser_page.get_by_test_id("feature-overview")
        expect(overview.get_by_role("heading", name="日常の塗布")).to_be_visible()
        overview.get_by_role("link", name="ノズル位置の設定", exact=False).click()

        expect(browser_page).to_have_url(f"{live_ui.base_url}/pasting/nozzle_cap")
        navigation = browser_page.get_by_role("navigation", name="はんだ塗布の機能")
        active = navigation.get_by_role("link", name="ノズル位置の設定", exact=False)
        expect(active).to_have_attribute("aria-current", "page")
        # 長い名前も省略せず、リンクの枠内で最後まで読める。
        assert active.evaluate("el => el.scrollWidth <= el.clientWidth")

    def test_mobile_menu_is_keyboard_accessible_and_survives_resize(
        self, live_ui: LiveUi, browser_page
    ):
        browser_page.set_viewport_size({"width": 390, "height": 844})
        browser_page.goto(f"{live_ui.base_url}/posctrl/camera_preview")
        menu = browser_page.get_by_test_id("feature-menu")
        navigation = browser_page.get_by_role("navigation", name="位置合わせの機能")
        expect(navigation).to_be_hidden()
        summary = menu.locator("summary")
        expect(summary).to_contain_text("カメラプレビュー")
        summary.focus()
        summary.press("Enter")
        expect(navigation).to_be_visible()
        navigation.get_by_role("link", name="銅箔検出調整", exact=True).click()
        expect(browser_page).to_have_url(f"{live_ui.base_url}/posctrl/copper_detection")
        expect(navigation).to_be_hidden()
        expect(summary).to_contain_text("銅箔検出調整")

        browser_page.set_viewport_size({"width": 1440, "height": 900})
        expect(navigation).to_be_visible()
        browser_page.set_viewport_size({"width": 390, "height": 844})
        expect(navigation).to_be_hidden()
        assert browser_page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
