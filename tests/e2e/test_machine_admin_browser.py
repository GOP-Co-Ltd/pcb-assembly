"""機体タブの設定・通知音・設定履歴とバックアップを実ブラウザで確かめる E2E."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from pcbasm.hal.fake import FAKE_AUDIO_DEVICES
from tests.support.browser import (
    E2E_MACHINE_ID as _E2E_MACHINE_ID,
    LiveServer,
    LiveUi,
    acquire_control as _acquire_control,
    wait_machine_field as _wait_machine_field,
)

_HTTP_TIMEOUT = 10.0

# 通知音ページで選択する候補（FakeAudioPlayer の 2 番目 = HifiBerry DAC）
_HIFIBERRY_DEVICE = FAKE_AUDIO_DEVICES[1]


def _open_settings_section(browser_page, live_ui, slug: str) -> None:
    """設定ページを開き、左ナビで目的のセクションを選ぶ.

    設定ページは選択中のセクションだけを描く（他は hidden）。

    ナビを押さずに入力へ触ろうとすると visible 待ちで固まる。
    """
    browser_page.goto(
        f"{live_ui.base_url}/machine_config", wait_until="domcontentloaded"
    )
    nav_item = browser_page.get_by_test_id(f"settings-nav-{slug}")
    nav_item.wait_for(state="visible", timeout=10_000)
    nav_item.click()


class TestSettingsOverBrowser:
    """設定画面の実ブラウザ操作."""

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [("probe.lift_height", 1.25)],
    )
    def test_probe_setting_autosave(
        self,
        live_server: LiveServer,
        live_ui: LiveUi,
        browser_page,
        field_name: str,
        value: float,
    ):
        _open_settings_section(browser_page, live_ui, "probe")
        _acquire_control(browser_page)
        field = browser_page.get_by_test_id(f"ms-{field_name}")
        field.wait_for(state="visible", timeout=10_000)

        field.fill(str(value))

        _wait_machine_field(live_server.base_url, field_name, value)

    def test_filter_reaches_settings_of_other_sections_without_control(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        """絞り込みはセクション横断で効き、操作権が無くても使える.

        ナビと絞り込みはフォームの外にあるので、閲覧だけの端末でも設定を探せる。

        フォームの中に置くと control.js がまとめて操作不可にする。
        """
        browser_page.goto(
            f"{live_ui.base_url}/machine_config", wait_until="domcontentloaded"
        )
        filter_box = browser_page.get_by_test_id("settings-filter")
        filter_box.wait_for(state="visible", timeout=10_000)

        filter_box.fill("min_samples")

        # 既定で開くのは先頭セクションだけ。他セクションの項目も絞り込みには出る
        browser_page.get_by_test_id("ms-probe.min_samples").wait_for(
            state="visible", timeout=10_000
        )
        # 先頭セクションに居座る非マッチ項目は消える（絞り込みが効いている証拠。
        # 初期状態で既に hidden な他セクションの項目を見ても空振りになる）
        browser_page.get_by_test_id("ms-solder.nozzle.diameter").wait_for(
            state="hidden", timeout=10_000
        )

    def test_section_selection_survives_filtering_and_history(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        """絞り込みを消したら直前に選んでいたセクションへ戻り、戻る操作でも切り替わる."""
        _open_settings_section(browser_page, live_ui, "camera")
        camera_field = browser_page.get_by_test_id("ms-camera.width")
        camera_field.wait_for(state="visible", timeout=10_000)

        filter_box = browser_page.get_by_test_id("settings-filter")
        filter_box.fill("min_samples")
        camera_field.wait_for(state="hidden", timeout=10_000)
        filter_box.fill("")

        # 先頭セクションへ飛ばず、選んでいた camera のまま
        camera_field.wait_for(state="visible", timeout=10_000)

        browser_page.get_by_test_id("settings-nav-audio").click()
        browser_page.get_by_test_id("ms-audio.volume").wait_for(
            state="visible", timeout=10_000
        )
        browser_page.go_back()

        camera_field.wait_for(state="visible", timeout=10_000)

    def test_reference_point_offset_pair_autosave(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        """float_pair 入力（X/Y 2 連）の編集が [x, y] 配列として保存される."""
        _open_settings_section(browser_page, live_ui, "reference_point")
        _acquire_control(browser_page)
        pair = browser_page.get_by_test_id("ms-reference_point.offsets.top_left")
        x_input = pair.locator('[data-pair-index="0"]')
        y_input = pair.locator('[data-pair-index="1"]')
        x_input.wait_for(state="visible", timeout=10_000)

        x_input.fill("6.5")
        y_input.fill("-4.5")

        _wait_machine_field(
            live_server.base_url, "reference_point.offsets.top_left", [6.5, -4.5]
        )

    def test_nozzle_position_saves_three_coordinates_together(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        """座標は X/Y/Z の 3 つがそろって初めて 1 回で送る（一部だけの座標は書かない）."""
        _open_settings_section(browser_page, live_ui, "solder")
        _acquire_control(browser_page)
        triple = browser_page.get_by_test_id("ms-solder.nozzle.cap.position")
        inputs = [triple.locator(f'[data-pair-index="{index}"]') for index in range(3)]
        inputs[0].wait_for(state="visible", timeout=10_000)

        for control, value in zip(inputs, ("10.5", "20.5", "-3.5"), strict=True):
            control.fill(value)

        _wait_machine_field(
            live_server.base_url,
            "solder.nozzle.cap.position",
            [10.5, 20.5, -3.5],
        )


class TestAudioPageOverBrowser:
    """通知音ページ（/audio）の実ブラウザ操作.

    デバイス一覧はサーバ（FakeAudioPlayer + selectable_devices）が返した値だけを
    描画し、保存は汎用即保存フォーム（settings.js）に委ねる。
    """

    def test_device_and_volume_are_saved_to_machine_toml(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        device = _HIFIBERRY_DEVICE.name
        browser_page.goto(f"{live_ui.base_url}/audio", wait_until="domcontentloaded")
        _acquire_control(browser_page)
        select = browser_page.get_by_test_id("audio-device")
        # option はサーバ応答から生成されるので、描画完了を待ってから操作する
        select.locator(f'option[value="{device}"]').wait_for(
            state="attached", timeout=10_000
        )
        assert select.locator("option").count() == len(FAKE_AUDIO_DEVICES)

        select.select_option(device)

        _wait_machine_field(live_server.base_url, "audio.device", device)

        volume = browser_page.get_by_test_id("audio-volume")
        volume.fill("0.4")

        _wait_machine_field(live_server.base_url, "audio.volume", 0.4)
        expect(browser_page.get_by_test_id("audio-volume-value")).to_have_text("40%")


class TestConfigHistoryOverBrowser:
    """設定履歴とバックアップの画面（/config_history）の実ブラウザ操作."""

    def test_restore_returns_to_the_content_before_a_save(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        path = live_server.settings.config_dir / "machine.toml"
        original = path.read_bytes()
        response = httpx.put(
            f"{live_server.base_url}/api/machine_config",
            json={"values": {"solder.motion.max_fill_speed": 0.9}},
            timeout=_HTTP_TIMEOUT,
        )
        assert response.status_code == 200, response.text
        browser_page.goto(
            f"{live_ui.base_url}/config_history", wait_until="domcontentloaded"
        )
        _acquire_control(browser_page)
        entries = browser_page.get_by_test_id("config-history-list").locator(
            ":scope > li"
        )
        expect(entries).to_have_count(1)
        expect(entries.first).to_contain_text("solder.motion.max_fill_speed: 0.8 → 0.9")

        browser_page.once("dialog", lambda dialog: dialog.accept())
        entries.first.get_by_role("button").click()

        # 復元する前の内容が新しい世代として先頭に増える
        expect(entries).to_have_count(2)
        expect(entries.first).to_contain_text("solder.motion.max_fill_speed: 0.9 → 0.8")
        assert path.read_bytes() == original

    def test_backup_link_downloads_a_zip(self, live_ui: LiveUi, browser_page):
        browser_page.goto(
            f"{live_ui.base_url}/config_history", wait_until="domcontentloaded"
        )

        with browser_page.expect_download() as info:
            browser_page.get_by_test_id("backup-download").click()

        assert info.value.suggested_filename.startswith(
            f"pcbasm-backup-{_E2E_MACHINE_ID}-"
        )
        assert info.value.suggested_filename.endswith(".zip")
