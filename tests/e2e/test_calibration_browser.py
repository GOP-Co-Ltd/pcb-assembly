"""校正タブのページ（銅箔検出・ノズル位置・吐出量と塗布量の校正）を実ブラウザで確かめる E2E."""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from pcbasm.pasting.paste_volume.calibration import CALIBRATION_SUFFIX
from pcbasm.paths import PROJECT_ROOT
from tests.support.browser import (
    LiveServer,
    LiveUi,
    acquire_control as _acquire_control,
    session_headers as _session_headers,
    wait_machine_field as _wait_machine_field,
)
from tests.support.paths import PASTE_VOLUME_CALIBRATION

_HTTP_TIMEOUT = 10.0


class TestCopperDetectionOverBrowser:
    """銅箔前処理スライダーを実ブラウザで保存し、backend の設定 API まで通す."""

    def test_save_persists_canny_thresholds(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        browser_page.goto(
            f"{live_ui.base_url}/copper_detection",
            wait_until="domcontentloaded",
        )
        _acquire_control(browser_page)

        for testid, value in (
            ("canny-low", "91"),
            ("canny-high", "193"),
        ):
            browser_page.get_by_test_id(testid).evaluate(
                """(element, value) => {
                    element.value = value;
                    element.dispatchEvent(new Event("input", { bubbles: true }));
                }""",
                value,
            )

        browser_page.get_by_test_id("canny-save").click()

        _wait_machine_field(live_server.base_url, "alignment.canny_low", 91.0)
        _wait_machine_field(live_server.base_url, "alignment.canny_high", 193.0)


class TestNozzlePositionsOverBrowser:
    """ノズル位置ページ（キャップ / クリーニング同居）を実ブラウザで通す."""

    def test_both_sections_render_without_id_collision(
        self, live_ui: LiveUi, browser_page
    ):
        browser_page.goto(
            f"{live_ui.base_url}/nozzle_cap", wait_until="domcontentloaded"
        )

        for testid in (
            "nozzle-cap-current",
            "nozzle-cap-record",
            "nozzle-clean-current",
            "nozzle-clean-record",
            "nozzle-clean-test",
            "nozzle-clean-settings",
        ):
            assert browser_page.get_by_test_id(f"{testid}").count() == 1

    def test_clean_record_button_is_gated_by_control(
        self, live_ui: LiveUi, browser_page
    ):
        """新しいボタンにも操作権ゲートの配線が効いている.

        無効化手段は `inert` 属性なので、`is_disabled()` ではなく属性を読む。
        """
        browser_page.goto(
            f"{live_ui.base_url}/nozzle_cap", wait_until="domcontentloaded"
        )
        button = browser_page.get_by_test_id("nozzle-clean-record")
        button.wait_for(state="visible", timeout=10_000)
        is_inert = "(el) => el.hasAttribute('inert')"

        assert button.evaluate(is_inert)

        _acquire_control(browser_page)

        assert not button.evaluate(is_inert)

    def test_clean_test_runs_as_job_on_the_same_page(
        self, live_ui: LiveUi, browser_page
    ):
        """テスト実行はジョブとして始まり、経過と結果を同じページのコンソールに出す.

        E2E の backend は位置が未記録なのでジョブは失敗する。終われば同じボタンでもう一度始められる。
        """
        browser_page.goto(
            f"{live_ui.base_url}/nozzle_cap", wait_until="domcontentloaded"
        )
        _acquire_control(browser_page)
        button = browser_page.get_by_test_id("nozzle-clean-test")
        button.wait_for(state="visible", timeout=10_000)

        button.click()

        expect(browser_page.get_by_test_id("jc-status")).to_have_attribute(
            "data-status", "failed", timeout=60_000
        )
        expect(browser_page.get_by_test_id("jc-log")).to_contain_text(
            "ノズルクリーニング位置が未記録です"
        )
        expect(button).to_be_enabled()

    def test_clean_setting_autosave_from_the_feature_page(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        """設定ページではなく feature ページのフォームで即保存する.

        settings.js の script タグ忘れはこの経路でしか検出できない。
        """
        browser_page.goto(
            f"{live_ui.base_url}/nozzle_cap", wait_until="domcontentloaded"
        )
        _acquire_control(browser_page)
        field = browser_page.get_by_test_id(
            "nozzle-clean-paste_dispenser.nozzle_clean.press_depth"
        )
        field.wait_for(state="visible", timeout=10_000)

        field.fill("0.4")

        _wait_machine_field(
            live_server.base_url, "paste_dispenser.nozzle_clean.press_depth", 0.4
        )


class TestDispenseCalibrationOverBrowser:
    """吐出量キャリブレーション統合ジョブ画面の実ブラウザ操作.

    SSR の静的属性（`data-loading-stage` / `data-runtime-editable` / ボタン id）は
    tests/web/ui/test_pages.py が見る。ここは `calibration_menu.js` が付ける初期状態
    だけを実ブラウザで確認する。
    """

    def test_menu_buttons_are_disabled_before_any_job(
        self, live_ui: LiveUi, browser_page
    ):
        browser_page.goto(
            f"{live_ui.base_url}/dispense_calibration",
            wait_until="domcontentloaded",
        )
        browser_page.get_by_test_id("calibration-menu").wait_for(
            state="visible", timeout=10_000
        )

        # ①②③/全実行/終了 のメニューボタン（ジョブ未実行なので全て disabled）
        for testid in (
            "calib-rotations-per-ul",
            "calib-max-dispense-rate",
            "calib-max-fill-speed",
            "calib-all",
            "calib-finish",
        ):
            expect(browser_page.get_by_test_id(testid)).to_be_disabled()


class TestPasteVolumeCalibrationSelect:
    """収集フォームの volume_calibration がブラウザ上で <select> になる.

    ここはサーバー側テストでは捕まらない。

    ParamSpec と persisted_params が揃っても、JS が値を落とせば毎回選び直しになる。

    忘れると無言で検証されないまま収集が終わる（実際に一度そうなった）。
    """

    def _write_calibration(self, live_server: LiveServer, stem: str) -> str:
        """保存済み校正をピン fixture から 1 件置き、ファイル名を返す."""
        root = live_server.settings.paste_volume_calibration_dir
        root.mkdir(parents=True, exist_ok=True)
        name = f"{stem}{CALIBRATION_SUFFIX}"
        (root / name).write_text(
            (PASTE_VOLUME_CALIBRATION).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return name

    def test_the_text_input_becomes_a_select_that_shows_its_conditions(
        self, live_ui: LiveUi, live_server: LiveServer, browser_page
    ):
        name = self._write_calibration(live_server, "s3x70-n030-h020")

        browser_page.goto(
            f"{live_ui.base_url}/paste_volume_calibration",
            wait_until="networkidle",
        )

        field = browser_page.get_by_test_id("param-volume_calibration")
        expect(field).to_have_count(1)
        assert field.evaluate("el => el.tagName") == "SELECT"
        assert name in field.evaluate("el => Array.from(el.options).map(o => o.value)")

        field.select_option(name)

        details = browser_page.get_by_test_id("volume-calibration-details")
        expect(details).to_contain_text("ノズル")
        expect(details).to_contain_text("総体積誤差")

    def test_a_saved_choice_survives_a_reload(
        self, live_ui: LiveUi, live_server: LiveServer, browser_page
    ):
        """persisted_params が UI へ届く（1 時間の収集ごとに選び直させない）."""
        name = self._write_calibration(live_server, "s3x70-n030-h020")
        browser_page.goto(
            f"{live_ui.base_url}/paste_volume_calibration",
            wait_until="networkidle",
        )
        _acquire_control(browser_page)
        response = httpx.post(
            f"{live_server.base_url}/api/jobs/paste_volume_calibration"
            "/param-defaults",
            json={"values": {"volume_calibration": name}},
            headers=_session_headers(browser_page),
            timeout=_HTTP_TIMEOUT,
        )
        assert response.status_code == 200, response.text

        browser_page.reload(wait_until="networkidle")

        assert (
            browser_page.get_by_test_id("param-volume_calibration").input_value()
            == name
        )
