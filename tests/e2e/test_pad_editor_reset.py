"""保存済みの pad 設定が読めなくなっても、画面の初期化で pad 編集へ戻れる。"""

from playwright.sync_api import expect

from pcbasm.pasting.overrides import BoardOverrides
from tests.support.browser import (
    SELECTED_LED_BLINKER,
    LiveServer,
    LiveUi,
    acquire_control,
    get_pad_config,
    select_led_blinker,
)


class TestPadEditorReset:
    def test_reset_recovers_from_unreadable_settings(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        select_led_blinker(live_server)
        data_dir = live_server.settings.webui_data_dir
        board_id = BoardOverrides(data_dir).board_id(SELECTED_LED_BLINKER)
        path = data_dir / "board_settings" / f"{board_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        page = browser_page
        page.goto(f"{live_ui.base_url}/paste_solder", wait_until="domcontentloaded")
        acquire_control(page)
        expect(page.locator('[data-testid="pad-editor-empty"]')).to_contain_text(
            "読めません"
        )

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator('[data-testid="pad-reset-config"]').click()

        expect(page.locator('[data-testid="pad-editor-body"]')).to_be_visible()
        expect(page.locator('[data-testid="pad-reset-config"]')).to_be_hidden()
        assert get_pad_config(live_server)["overrides"] == {}
