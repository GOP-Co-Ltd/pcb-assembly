"""流量キャリブレーション基板生成ページの実ブラウザE2E."""

import json
from pathlib import Path

from playwright.sync_api import expect

from pcbasm.pcb import PcbFile
from tests.e2e.conftest import LiveUi

_BROWSER_TIMEOUT_MS = 15_000


def _open_board_generator(page, live_ui: LiveUi) -> None:
    page.goto(
        f"{live_ui.base_url}/pasting/paste_flow_calibration_board",
        wait_until="domcontentloaded",
    )
    expect(page.locator("#pfc-pattern-rows tr")).to_have_count(
        6, timeout=_BROWSER_TIMEOUT_MS
    )
    expect(page.locator("#pfc-preview-status")).to_have_text(
        "配置可能です", timeout=_BROWSER_TIMEOUT_MS
    )


class TestPasteFlowCalibrationBoardBrowser:
    def test_preview_tracks_table_edits_add_remove_and_overflow(
        self, live_ui: LiveUi, browser_page
    ):
        _open_board_generator(browser_page, live_ui)

        assert browser_page.locator(".pfc-paste").count() > 0
        assert browser_page.locator(".pfc-copper").count() > 0
        assert browser_page.locator(".pfc-group-boundary").count() == 6

        rotation_count = browser_page.locator(
            '[data-catalog-id="r_0402_1005metric"] '
            '[data-pattern-field="rotation_count"]'
        )
        rotation_count.fill("2")
        expect(
            browser_page.locator(
                '[data-catalog-id="r_0402_1005metric"] .pfc-resolved-angles'
            )
        ).to_have_text("0°, 90°", timeout=_BROWSER_TIMEOUT_MS)

        browser_page.locator("#pfc-board-width").fill("10")
        expect(browser_page.locator("#pfc-preview-status")).to_contain_text(
            "超え", timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator("#pfc-preview > *")).to_have_count(0)
        expect(browser_page.locator("#pfc-generate")).to_be_disabled()
        expect(browser_page.locator("#pfc-export")).to_be_enabled()

        browser_page.locator("#pfc-board-width").fill("40")
        expect(browser_page.locator("#pfc-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )

        browser_page.locator("#pfc-catalog-select").select_option("soic_8")
        browser_page.locator("#pfc-add-pattern").click()
        expect(browser_page.locator("#pfc-pattern-rows tr")).to_have_count(7)
        expect(browser_page.locator("#pfc-preview-status")).to_contain_text(
            "基板高さ", timeout=_BROWSER_TIMEOUT_MS
        )

        browser_page.locator('[data-catalog-id="soic_8"] button').click()
        expect(browser_page.locator("#pfc-pattern-rows tr")).to_have_count(6)
        expect(browser_page.locator("#pfc-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )

    def test_export_import_and_kicad_download_need_no_control(
        self, live_ui: LiveUi, browser_page, tmp_path: Path
    ):
        _open_board_generator(browser_page, live_ui)
        assert browser_page.locator("body").get_attribute("data-control") == "free"
        assert not browser_page.locator("#pfc-generate").evaluate(
            "element => element.hasAttribute('inert')"
        )

        with browser_page.expect_download(timeout=_BROWSER_TIMEOUT_MS) as export_info:
            browser_page.locator("#pfc-export").click()
        exported = export_info.value
        assert exported.suggested_filename == (
            "pcbasm-paste-flow-calibration-board.json"
        )
        config_path = tmp_path / exported.suggested_filename
        exported.save_as(config_path)
        document = json.loads(config_path.read_text(encoding="utf-8"))
        assert document["kind"] == "paste_flow_calibration_board"

        browser_page.locator("#pfc-board-width").fill("42")
        expect(browser_page.locator("#pfc-board-width")).to_have_value(
            "42", timeout=_BROWSER_TIMEOUT_MS
        )
        browser_page.locator("#pfc-import-file").set_input_files(config_path)
        expect(browser_page.locator("#pfc-board-width")).to_have_value(
            "40", timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator("#pfc-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )

        with browser_page.expect_download(timeout=_BROWSER_TIMEOUT_MS) as board_info:
            browser_page.locator("#pfc-generate").click()
        downloaded = board_info.value
        assert downloaded.suggested_filename == (
            "pcbasm-paste-flow-calibration-board.kicad_pcb"
        )
        board_path = tmp_path / downloaded.suggested_filename
        downloaded.save_as(board_path)
        pcb = PcbFile(board_path)
        assert pcb.outline.width == 40.0
        assert len(pcb.components) == 65
