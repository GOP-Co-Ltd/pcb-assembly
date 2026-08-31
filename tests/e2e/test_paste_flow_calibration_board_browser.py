"""流量キャリブレーション基板生成ページの実ブラウザE2E."""

import json
from pathlib import Path

from playwright.sync_api import expect

from pcbasm.pcb import PcbFile
from tests.e2e.conftest import LiveUi

_BROWSER_TIMEOUT_MS = 15_000
_QFN = "Package_DFN_QFN.pretty/QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
_QFN_NAME = "QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
_QFN_LABEL = f"Package_DFN_QFN / {_QFN_NAME}"


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
        expect(browser_page.locator("#pfc-preview-summary")).to_have_text(
            "64パッド + purge pad"
        )
        expect(browser_page.locator(".pfc-pattern-table thead")).to_contain_text(
            "回転分割数"
        )
        expect(browser_page.locator(".pfc-pattern-table thead")).to_contain_text(
            "繰り返し数"
        )
        expect(browser_page.locator(".pfc-pattern-table thead")).to_contain_text(
            "転置配置"
        )
        expect(browser_page.locator(".pfc-pattern-table thead")).to_contain_text("名称")
        expect(browser_page.locator("#pfc-auto-pack")).to_be_checked()
        expect(browser_page.locator("#pfc-footprint-results option")).to_have_count(
            69, timeout=_BROWSER_TIMEOUT_MS
        )

        config_box = browser_page.locator(".pfc-config-card").bounding_box()
        preview_box = browser_page.locator(".pfc-preview-card").bounding_box()
        assert config_box is not None
        assert preview_box is not None
        assert preview_box["y"] >= config_box["y"] + config_box["height"]

        first_row = browser_page.locator("#pfc-pattern-rows tr").first
        transpose = first_row.locator('[data-pattern-field="transpose"]')
        expect(transpose).not_to_be_checked()
        expect(transpose).to_be_disabled()
        browser_page.locator("#pfc-auto-pack").uncheck()
        expect(transpose).to_be_enabled(timeout=_BROWSER_TIMEOUT_MS)
        resolved_size = first_row.locator(".pfc-resolved-size")
        original_size = resolved_size.inner_text()
        transpose.check()
        expect(resolved_size).not_to_have_text(
            original_size, timeout=_BROWSER_TIMEOUT_MS
        )
        rotation_count = first_row.locator('[data-pattern-field="rotation_count"]')
        rotation_count.fill("2")
        expect(first_row.locator(".pfc-resolved-angles")).to_have_text(
            "0°, 90°", timeout=_BROWSER_TIMEOUT_MS
        )
        rotation_count.fill("4")
        expect(first_row.locator(".pfc-resolved-angles")).to_have_text(
            "0°, 45°, 90°, 135°", timeout=_BROWSER_TIMEOUT_MS
        )

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

        name_sort = browser_page.locator('[data-sort-field="name"]')
        name_sort.click()
        name_sort.click()
        expect(name_sort.locator("xpath=..")).to_have_attribute(
            "aria-sort", "descending"
        )
        expect(
            browser_page.locator("#pfc-pattern-rows .pfc-name-label").first
        ).to_contain_text("SOT-23-5")
        pad_sort = browser_page.locator('[data-sort-field="pad"]')
        pad_sort.click()
        expect(pad_sort.locator("xpath=..")).to_have_attribute("aria-sort", "ascending")

        browser_page.locator("#pfc-footprint-search").fill(
            "QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
        )
        expect(
            browser_page.locator("#pfc-footprint-results option").first
        ).to_have_attribute("value", _QFN, timeout=_BROWSER_TIMEOUT_MS)
        selected_option = browser_page.locator(
            f'#pfc-footprint-results option[value="{_QFN}"]'
        )
        expect(selected_option).to_have_attribute("title", _QFN_LABEL)
        assert selected_option.evaluate("element => element.textContent.endsWith('…')")
        browser_page.locator("#pfc-footprint-results").select_option(_QFN)
        expect(browser_page.locator("#pfc-footprint-results")).to_have_attribute(
            "title", _QFN_LABEL
        )
        browser_page.locator("#pfc-add-pattern").click()
        expect(browser_page.locator("#pfc-pattern-rows tr")).to_have_count(
            9, timeout=_BROWSER_TIMEOUT_MS
        )
        qfn_rows = browser_page.locator("#pfc-pattern-rows tr").filter(
            has_text="QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
        )
        expect(qfn_rows).to_have_count(3)
        expect(qfn_rows.filter(has_text="Paste aperture")).to_have_count(1)
        expect(qfn_rows.filter(has_text="Pad 1–16")).to_have_count(1)
        expect(qfn_rows.filter(has_text="Pad 17")).to_have_count(1)
        qfn_name = qfn_rows.first.locator(".pfc-name-label .pfc-truncated-text")
        expect(qfn_name).to_have_attribute("title", _QFN_NAME)
        assert qfn_name.evaluate("element => element.scrollWidth > element.clientWidth")
        expect(browser_page.locator("#pfc-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )

        expect(browser_page.locator("#pfc-preview-summary")).to_have_text(
            "88パッド + purge pad"
        )
        expect(browser_page.locator(".pfc-group-boundary")).to_have_count(9)

        for _ in range(3):
            qfn_rows.first.locator("button").click()
        expect(browser_page.locator("#pfc-pattern-rows tr")).to_have_count(
            6, timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator("#pfc-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )

        browser_page.locator("#pfc-custom-pad-shape").select_option("oval")
        browser_page.locator("#pfc-custom-pad-width").fill("1.5")
        browser_page.locator("#pfc-custom-pad-height").fill("0.5")
        browser_page.locator("#pfc-add-custom-pad").click()
        expect(browser_page.locator("#pfc-pattern-rows tr")).to_have_count(
            7, timeout=_BROWSER_TIMEOUT_MS
        )
        custom_row = browser_page.locator("#pfc-pattern-rows tr").filter(
            has_text="長円（スロット） 1.5 × 0.5 mm"
        )
        expect(custom_row).to_have_count(1)
        expect(custom_row).to_contain_text("長円（スロット）")
        expect(browser_page.locator("#pfc-preview-summary")).to_have_text(
            "76パッド + purge pad", timeout=_BROWSER_TIMEOUT_MS
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
        assert document["schema_version"] == 3
        assert document["auto_pack"] is True
        assert document["custom_pads"] == []
        assert all(not pattern["transpose"] for pattern in document["patterns"])

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
        assert len(pcb.pads) == 65
