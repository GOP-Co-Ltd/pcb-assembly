"""実ブラウザ（Playwright + Chromium）で検証する WebUI 操作の E2E.

`make test-e2e` で実行する。pad editor 専用のブラウザテストは
tests/e2e/test_paste_solder_browser.py、HTTP / WS / MJPEG の純粋な 実ネットワーク検証は
tests/e2e/test_webui_e2e.py に分担する。
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests.e2e.conftest import (
    TERMINAL as _TERMINAL,
    LiveServer,
    select_led_blinker as _select_led_blinker,
    wait_machine_field as _wait_machine_field,
)
from tests.helpers import wait_until

_HTTP_TIMEOUT = 10.0


def _current_job(base_url: str) -> dict | None:
    """GET /api/jobs/current の job（無ければ None）を返す."""
    response = httpx.get(f"{base_url}/api/jobs/current", timeout=_HTTP_TIMEOUT)
    assert response.status_code == 200
    return response.json()["job"]


class TestPromptDialogOverBrowser:
    """実ブラウザ上の prompt modal 表示。"""

    def test_confirm_dialog_uses_custom_button_labels(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)

        browser_page.goto(
            f"{live_server.base_url}/pasting/height_plane",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#job-console").wait_for(state="visible", timeout=10_000)

        start = httpx.post(
            f"{live_server.base_url}/api/jobs/height_plane", timeout=_HTTP_TIMEOUT
        )
        assert start.status_code == 201, start.text

        dialog = browser_page.locator("#jc-prompt")
        dialog.wait_for(state="visible", timeout=30_000)
        ok_button = browser_page.locator("#jc-prompt-ok")
        no_button = browser_page.locator("#jc-prompt-no")
        ok_button.wait_for(state="visible", timeout=10_000)
        no_button.wait_for(state="visible", timeout=10_000)

        assert ok_button.inner_text() == "続行"
        assert no_button.inner_text() == "中止"

        no_button.click()
        wait_until(
            lambda: (job := _current_job(live_server.base_url)) is not None
            and job["status"] == "aborted",
            timeout=10.0,
            interval=0.05,
        )

    def test_enter_key_submits_ok_instead_of_cancel(
        self, live_server: LiveServer, browser_page
    ):
        """Enter の暗黙送信は OK（続行）に落ちる.

        中止が DOM 先頭の submit ボタンだと Enter が中止を押した扱いになり、
        確認や質量入力のたびにジョブ/サブキャリブが勝手に中止されていた。 続行（True）ならセットアップへ進み、Klipper
        不通（port 7126）で failed になる。旧実装（中止が既定）だと即 aborted になっていた。
        """
        _select_led_blinker(live_server)

        browser_page.goto(
            f"{live_server.base_url}/pasting/height_plane",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#job-console").wait_for(state="visible", timeout=10_000)

        start = httpx.post(
            f"{live_server.base_url}/api/jobs/height_plane", timeout=_HTTP_TIMEOUT
        )
        assert start.status_code == 201, start.text

        browser_page.locator("#jc-prompt").wait_for(state="visible", timeout=30_000)
        browser_page.keyboard.press("Enter")

        wait_until(
            lambda: (job := _current_job(live_server.base_url)) is not None
            and job["status"] in _TERMINAL,
            timeout=120.0,
            interval=0.1,
        )
        current = _current_job(live_server.base_url)
        assert current is not None
        assert current["status"] == "failed", current


class TestSettingsOverBrowser:
    """設定画面の実ブラウザ操作."""

    def test_probe_lift_height_autosave(self, live_server: LiveServer, browser_page):
        browser_page.goto(
            f"{live_server.base_url}/settings", wait_until="domcontentloaded"
        )
        field = browser_page.locator('input[name="probe.lift_height"]')
        field.wait_for(state="visible", timeout=10_000)

        field.fill("1.25")

        _wait_machine_field(live_server.base_url, "probe.lift_height", 1.25)

    def test_reference_point_offset_pair_autosave(
        self, live_server: LiveServer, browser_page
    ):
        """float_pair 入力（X/Y 2 連）の編集が [x, y] 配列として保存される."""
        browser_page.goto(
            f"{live_server.base_url}/settings", wait_until="domcontentloaded"
        )
        pair = 'input[data-pair-key="reference_point.offset"]'
        x_input = browser_page.locator(f'{pair}[data-pair-index="0"]')
        y_input = browser_page.locator(f'{pair}[data-pair-index="1"]')
        x_input.wait_for(state="visible", timeout=10_000)

        x_input.fill("6.5")
        y_input.fill("-4.5")

        _wait_machine_field(live_server.base_url, "reference_point.offset", [6.5, -4.5])

    def test_reference_point_corner_select_autosave(
        self, live_server: LiveServer, browser_page
    ):
        """アンカーコーナー select の変更が corner 値として自動保存される."""
        browser_page.goto(
            f"{live_server.base_url}/settings", wait_until="domcontentloaded"
        )
        select = browser_page.locator('select[name="reference_point.corner"]')
        select.wait_for(state="visible", timeout=10_000)

        select.select_option("bottom_right")

        _wait_machine_field(
            live_server.base_url, "reference_point.corner", "bottom_right"
        )

    def test_setting_label_does_not_focus_input(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/settings", wait_until="domcontentloaded"
        )
        label = browser_page.locator(".settings-label").nth(0)
        label.wait_for(state="visible", timeout=10_000)

        label.click()

        active_tag = browser_page.evaluate("document.activeElement?.tagName")
        assert active_tag != "INPUT"


class TestLoadingOverBrowser:
    """ペーストローディング画面の実ブラウザ操作.

    質量キャリブレーション表（初期 rotations_per_ul 等のブートストラップ用）と、 押出/吸引の操作パネル +
    パラメータ同期を持つ。既存値を線引きで補正する dispense_calibration とは用途が別なので併存する。
    """

    def test_loading_controls_sync_inputs_to_hidden_params(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/pasting/loading",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#loading-controls").wait_for(
            state="visible", timeout=10_000
        )

        browser_page.locator("#lc-amount").fill("0.2")
        browser_page.locator("#lc-rotations").fill("5")
        browser_page.locator("#lc-rate").fill("0.5")
        browser_page.locator("#lc-accel").fill("0.5")

        # ローディング操作パネルの hidden へ各入力が同期される
        assert browser_page.locator("#param-amount").input_value() == "0.2"
        assert browser_page.locator("#param-rotations").input_value() == "5"
        assert browser_page.locator("#param-rate").input_value() == "0.5"
        assert browser_page.locator("#param-accel").input_value() == "0.5"

    def test_loading_inputs_persist_across_reload(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/pasting/loading",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#loading-controls").wait_for(
            state="visible", timeout=10_000
        )

        browser_page.locator("#lc-amount").fill("0.33")
        browser_page.locator("#lc-rotations").fill("6.5")
        browser_page.locator("#lc-rate").fill("1.25")
        # 最後の入力が起こす debounce 即保存 POST を待ってからリロードする
        # （「実行」していないので、即保存が効いていなければ値は失われる）
        with browser_page.expect_response(
            lambda r: "/param-defaults" in r.url and r.request.method == "POST"
        ):
            browser_page.locator("#lc-accel").fill("2.5")

        browser_page.reload(wait_until="domcontentloaded")
        browser_page.locator("#loading-controls").wait_for(
            state="visible", timeout=10_000
        )

        expect(browser_page.locator("#lc-amount")).to_have_value("0.33")
        expect(browser_page.locator("#lc-rotations")).to_have_value("6.5")
        expect(browser_page.locator("#lc-rate")).to_have_value("1.25")
        expect(browser_page.locator("#lc-accel")).to_have_value("2.5")

    def test_mass_calibration_calculates_and_applies_dispense_values(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/pasting/loading",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#loading-mass-calibration").wait_for(
            state="visible", timeout=10_000
        )

        browser_page.locator("#lc-amount").fill("0.2")
        browser_page.locator("#lc-rotations").fill("5")
        browser_page.locator("#lc-rate").fill("0.5")
        browser_page.locator("#lc-accel").fill("0.5")
        browser_page.locator("#lc-mass-mg").fill("10")

        # ローディング操作パネルの hidden へ各入力が同期される
        assert browser_page.locator("#param-amount").input_value() == "0.2"
        assert browser_page.locator("#param-rotations").input_value() == "5"
        assert browser_page.locator("#param-rate").input_value() == "0.5"
        assert browser_page.locator("#param-accel").input_value() == "0.5"

        # 算出値は debounce GET で非同期に届くので Playwright の自動待機で待つ。
        # mass=10, rotations=5, density=3.78 → volume=2.645503, rpu=1.890000,
        # rate/accel = 0.5/1.89 = 0.264550（toFixed(6) 表示）
        expect(browser_page.locator("#lc-volume-ul")).to_have_text("2.645503")
        expect(browser_page.locator("#lc-rotations-per-ul")).to_have_text("1.890000")
        expect(browser_page.locator("#lc-dispense-rate")).to_have_text("0.264550")
        expect(browser_page.locator("#lc-dispense-accel")).to_have_text("0.264550")

        # 個別適用: rotations_per_ul のみ永続化 → 現在値 output が更新される
        browser_page.locator("#lc-apply-rotations-per-ul").click()
        _wait_machine_field(
            live_server.base_url, "paste_dispenser.rotations_per_ul", 1.89
        )
        expect(browser_page.locator("#lc-current-rotations-per-ul")).to_have_text(
            "1.890000"
        )

        # 一括適用: 3 キーがまとめて永続化される
        # 保存値は Number(toFixed(6)) のトリム後（1.89, 0.26455, 0.26455）
        browser_page.locator("#lc-apply-all").click()
        _wait_machine_field(
            live_server.base_url, "paste_dispenser.rotations_per_ul", 1.89
        )
        _wait_machine_field(
            live_server.base_url, "paste_dispenser.max_dispense_rate", 0.26455
        )
        _wait_machine_field(
            live_server.base_url, "paste_dispenser.dispense_accel", 0.26455
        )
        expect(browser_page.locator("#lc-current-dispense-rate")).to_have_text(
            "0.264550"
        )
        expect(browser_page.locator("#lc-current-dispense-accel")).to_have_text(
            "0.264550"
        )

        # 必須入力をクリア（mass=0）→ 4 出力が "-"、4 適用ボタンが全て無効化される
        browser_page.locator("#lc-mass-mg").fill("0")
        expect(browser_page.locator("#lc-volume-ul")).to_have_text("-")
        expect(browser_page.locator("#lc-rotations-per-ul")).to_have_text("-")
        expect(browser_page.locator("#lc-dispense-rate")).to_have_text("-")
        expect(browser_page.locator("#lc-dispense-accel")).to_have_text("-")
        expect(browser_page.locator("#lc-apply-rotations-per-ul")).to_be_disabled()
        expect(browser_page.locator("#lc-apply-dispense-rate")).to_be_disabled()
        expect(browser_page.locator("#lc-apply-dispense-accel")).to_be_disabled()
        expect(browser_page.locator("#lc-apply-all")).to_be_disabled()


class TestDispenseCalibrationOverBrowser:
    """吐出量キャリブレーション統合ジョブ画面の実ブラウザ表示."""

    def test_menu_and_loading_controls_render(
        self, live_server: LiveServer, browser_page
    ):
        browser_page.goto(
            f"{live_server.base_url}/pasting/dispense_calibration",
            wait_until="domcontentloaded",
        )
        browser_page.locator("#calibration-menu").wait_for(
            state="visible", timeout=10_000
        )

        # ①②③/全実行/終了 のメニューボタン（ジョブ未実行なので全て disabled）
        for button_id in (
            "#calib-rotations-per-ul",
            "#calib-max-dispense-rate",
            "#calib-max-fill-speed",
            "#calib-all",
            "#calib-finish",
        ):
            expect(browser_page.locator(button_id)).to_be_disabled()

        # プライム用 loading_controls はメニュー段階と ① 専用ローディング段階で有効化される設定
        panel = browser_page.locator("#loading-controls")
        panel.wait_for(state="visible", timeout=10_000)
        assert (
            panel.get_attribute("data-loading-stage")
            == "キャリブレーションメニュー,ローディング"
        )

        # 実行中変更可（runtime_editable）の入力には目印が付き、固定値には付かない
        assert (
            browser_page.locator("#param-line_length").get_attribute(
                "data-runtime-editable"
            )
            == "true"
        )
        assert (
            browser_page.locator("#param-board_width").get_attribute(
                "data-runtime-editable"
            )
            is None
        )
