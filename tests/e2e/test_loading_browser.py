"""ペーストローディングの画面を実ブラウザで確かめる E2E."""

from __future__ import annotations

from playwright.sync_api import expect

from tests.e2e.conftest import (
    LiveServer,
    LiveUi,
    acquire_control as _acquire_control,
    wait_machine_field as _wait_machine_field,
)


class TestLoadingOverBrowser:
    """ペーストローディング画面の実ブラウザ操作.

    質量キャリブレーション表（初期 rotations_per_ul 等のブートストラップ用）と、 押出/吸引の操作パネル +
    パラメータ同期を持つ。既存値を線引きで補正する dispense_calibration とは用途が別なので併存する。
    """

    def test_start_sends_only_specified_position_axes_and_begins_homing(
        self, live_ui: LiveUi, browser_page
    ):
        browser_page.goto(
            f"{live_ui.base_url}/loading",
            wait_until="domcontentloaded",
        )
        _acquire_control(browser_page)
        browser_page.locator("#param-position_x").fill("12.5")
        browser_page.locator("#param-position_z").fill("3")

        with browser_page.expect_response(
            lambda response: response.url.endswith("/api/jobs/loading")
            and response.request.method == "POST"
        ) as response_info:
            browser_page.locator("#job-run").click()

        response = response_info.value
        assert response.status == 201
        payload = response.request.post_data_json
        assert payload["params"]["position_x"] == 12.5
        assert "position_y" not in payload["params"]
        assert payload["params"]["position_z"] == 3

        expect(browser_page.locator("#jc-status")).to_have_text("失敗", timeout=60_000)
        expect(browser_page.locator("#jc-progress-text")).to_have_text("ホーミング")

    def test_loading_inputs_persist_across_reload(self, live_ui: LiveUi, browser_page):
        browser_page.goto(
            f"{live_ui.base_url}/loading",
            wait_until="domcontentloaded",
        )
        _acquire_control(browser_page)
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
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        browser_page.goto(
            f"{live_ui.base_url}/loading",
            wait_until="domcontentloaded",
        )
        _acquire_control(browser_page)
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
            live_server.base_url, "paste_dispenser.motion.max_dispense_rate", 0.26455
        )
        _wait_machine_field(
            live_server.base_url, "paste_dispenser.motion.dispense_accel", 0.26455
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
