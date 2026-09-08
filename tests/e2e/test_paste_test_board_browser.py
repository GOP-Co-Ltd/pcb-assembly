"""テスト塗布基板生成ページの実ブラウザE2E."""

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from pcbasm.pcb import PcbFile
from tests.e2e.conftest import LiveUi

_BROWSER_TIMEOUT_MS = 15_000
_QFN = "Package_DFN_QFN.pretty/QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
_QFN_NAME = "QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
_QFN_LABEL = f"Package_DFN_QFN / {_QFN_NAME}"
_OVERFLOW_MESSAGE = re.compile("超え|収まりません")


def _open_board_generator(page, live_ui: LiveUi) -> None:
    page.goto(
        f"{live_ui.base_url}/pasting/paste_test_board",
        wait_until="domcontentloaded",
    )
    expect(page.locator("#ptb-pattern-rows tr")).to_have_count(
        6, timeout=_BROWSER_TIMEOUT_MS
    )
    expect(page.locator("#ptb-preview-status")).to_have_text(
        "配置可能です", timeout=_BROWSER_TIMEOUT_MS
    )


class TestPasteTestBoardBrowser:
    def test_preview_tracks_table_edits_add_remove_and_overflow(
        self, live_ui: LiveUi, browser_page
    ):
        _open_board_generator(browser_page, live_ui)

        assert browser_page.locator(".ptb-paste").count() > 0
        assert browser_page.locator(".ptb-copper").count() > 0
        expect(browser_page.locator(".ptb-preview-pad[aria-label]")).to_have_count(64)
        named_pad = browser_page.locator(".ptb-preview-pad[aria-label]").first
        display_name = named_pad.get_attribute("aria-label")
        assert display_name is not None
        named_pad.hover()
        expect(named_pad.locator("title")).to_have_text(display_name)
        expect(browser_page.locator("#ptb-preview text")).to_have_count(0)
        expect(browser_page.locator("#ptb-preview-summary")).to_have_text(
            "64パッド + purge pad"
        )
        expect(browser_page.locator(".ptb-pattern-table thead")).to_contain_text(
            "回転分割数"
        )
        expect(browser_page.locator(".ptb-pattern-table thead")).to_contain_text(
            "繰り返し数"
        )
        expect(browser_page.locator(".ptb-pattern-table thead")).not_to_contain_text(
            "転置配置"
        )
        expect(browser_page.locator(".ptb-pattern-table thead")).to_contain_text("名称")
        expect(browser_page.locator("#ptb-auto-pack")).to_have_count(0)
        expect(browser_page.locator("#ptb-footprint-results option")).to_have_count(
            8, timeout=_BROWSER_TIMEOUT_MS
        )

        config_box = browser_page.locator(".ptb-config-card").bounding_box()
        preview_box = browser_page.locator(".ptb-preview-card").bounding_box()
        assert config_box is not None
        assert preview_box is not None
        assert preview_box["y"] >= config_box["y"] + config_box["height"]

        first_row = browser_page.locator("#ptb-pattern-rows tr").first
        rotation_count = first_row.locator('[data-pattern-field="rotation_count"]')
        rotation_count.fill("2")
        expect(first_row.locator(".ptb-resolved-angles")).to_have_text(
            "0°, 90°", timeout=_BROWSER_TIMEOUT_MS
        )
        rotation_count.fill("4")
        expect(first_row.locator(".ptb-resolved-angles")).to_have_text(
            "0°, 45°, 90°, 135°", timeout=_BROWSER_TIMEOUT_MS
        )

        browser_page.locator("#ptb-board-width").fill("10")
        expect(browser_page.locator("#ptb-preview-status")).to_contain_text(
            _OVERFLOW_MESSAGE, timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator("#ptb-preview-summary")).to_have_text(
            "64パッド + purge pad"
        )
        expect(browser_page.locator(".ptb-overflow-layer")).to_have_count(1)
        assert browser_page.locator(".ptb-overflow-shape").count() > 0
        overflow_color = browser_page.locator(".ptb-overflow-shape").first.evaluate(
            "element => getComputedStyle(element).fill"
        )
        red, green, blue = (int(value) for value in re.findall(r"\d+", overflow_color))
        assert red > green and red > blue
        overflow_layer = browser_page.locator(".ptb-overflow-layer")
        expect(overflow_layer).to_have_attribute(
            "clip-path", "url(#ptb-placement-overflow-clip)"
        )
        clip_membership = browser_page.locator(
            "#ptb-placement-overflow-clip path"
        ).evaluate(
            """path => ({
                placementCenter: path.isPointInFill(new DOMPoint(5, 20)),
                edgeMargin: path.isPointInFill(new DOMPoint(0.5, 20)),
            })"""
        )
        assert clip_membership == {
            "placementCenter": False,
            "edgeMargin": True,
        }
        expect(browser_page.locator("#ptb-generate")).to_be_disabled()
        expect(browser_page.locator("#ptb-export")).to_be_enabled()

        browser_page.locator("#ptb-board-width").fill("40")
        expect(browser_page.locator("#ptb-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator(".ptb-overflow-layer")).to_have_count(0)
        expect(browser_page.locator("#ptb-generate")).to_be_enabled()

        name_sort = browser_page.locator('[data-sort-field="name"]')
        name_sort.click()
        name_sort.click()
        expect(name_sort.locator("xpath=..")).to_have_attribute(
            "aria-sort", "descending"
        )
        expect(
            browser_page.locator("#ptb-pattern-rows .ptb-name-label").first
        ).to_contain_text("SOT-23-5")
        pad_sort = browser_page.locator('[data-sort-field="pad"]')
        pad_sort.click()
        expect(pad_sort.locator("xpath=..")).to_have_attribute("aria-sort", "ascending")

        browser_page.locator("#ptb-footprint-search").fill(
            "QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
        )
        expect(
            browser_page.locator("#ptb-footprint-results option").first
        ).to_have_attribute("value", _QFN, timeout=_BROWSER_TIMEOUT_MS)
        selected_option = browser_page.locator(
            f'#ptb-footprint-results option[value="{_QFN}"]'
        )
        expect(selected_option).to_have_attribute("title", _QFN_LABEL)
        assert selected_option.evaluate("element => element.textContent.endsWith('…')")
        browser_page.locator("#ptb-footprint-results").select_option(_QFN)
        expect(browser_page.locator("#ptb-footprint-results")).to_have_attribute(
            "title", _QFN_LABEL
        )
        browser_page.locator("#ptb-add-pattern").click()
        expect(browser_page.locator("#ptb-pattern-rows tr")).to_have_count(
            9, timeout=_BROWSER_TIMEOUT_MS
        )
        qfn_rows = browser_page.locator("#ptb-pattern-rows tr").filter(
            has_text="QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
        )
        expect(qfn_rows).to_have_count(3)
        expect(qfn_rows.filter(has_text="Paste aperture")).to_have_count(1)
        expect(qfn_rows.filter(has_text="Pad 1–16")).to_have_count(1)
        expect(qfn_rows.filter(has_text="Pad 17")).to_have_count(1)
        qfn_name = qfn_rows.first.locator(".ptb-name-label .ptb-truncated-text")
        expect(qfn_name).to_have_attribute("title", _QFN_NAME)
        assert qfn_name.evaluate("element => element.scrollWidth > element.clientWidth")
        expect(browser_page.locator("#ptb-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )

        expect(browser_page.locator("#ptb-preview-summary")).to_have_text(
            "88パッド + purge pad"
        )

        for _ in range(3):
            qfn_rows.first.locator("button").click()
        expect(browser_page.locator("#ptb-pattern-rows tr")).to_have_count(
            6, timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator("#ptb-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )

        browser_page.locator("#ptb-custom-pad-shape").select_option("oval")
        browser_page.locator("#ptb-custom-pad-width").fill("1.5")
        browser_page.locator("#ptb-custom-pad-height").fill("0.5")
        browser_page.locator("#ptb-add-custom-pad").click()
        expect(browser_page.locator("#ptb-pattern-rows tr")).to_have_count(
            7, timeout=_BROWSER_TIMEOUT_MS
        )
        custom_row = browser_page.locator("#ptb-pattern-rows tr").filter(
            has_text="長円（スロット） 1.5 × 0.5 mm"
        )
        expect(custom_row).to_have_count(1)
        expect(custom_row).to_contain_text("長円（スロット）")
        expect(browser_page.locator("#ptb-preview-summary")).to_have_text(
            "76パッド + purge pad", timeout=_BROWSER_TIMEOUT_MS
        )

    def test_empty_pattern_config_can_be_recovered(self, live_ui: LiveUi, browser_page):
        _open_board_generator(browser_page, live_ui)
        rows = browser_page.locator("#ptb-pattern-rows tr")

        while rows.count():
            rows.first.locator(".ptb-remove-pattern").click()

        expect(rows).to_have_count(0)
        expect(browser_page.locator("#ptb-preview-status")).to_contain_text(
            "1つ以上", timeout=_BROWSER_TIMEOUT_MS
        )
        browser_page.locator("#ptb-custom-pad-shape").select_option("circle")
        browser_page.locator("#ptb-custom-pad-width").fill("0.75")
        browser_page.locator("#ptb-add-custom-pad").click()

        expect(rows).to_have_count(1, timeout=_BROWSER_TIMEOUT_MS)
        expect(rows.first).to_contain_text("円 φ0.75 mm")
        expect(browser_page.locator("#ptb-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )

    def test_editing_draft_survives_reload(self, live_ui: LiveUi, browser_page):
        _open_board_generator(browser_page, live_ui)

        board_width = browser_page.locator("#ptb-board-width")
        preview_status = browser_page.locator("#ptb-preview-status")
        board_width.fill("10")
        expect(preview_status).to_contain_text(
            _OVERFLOW_MESSAGE, timeout=_BROWSER_TIMEOUT_MS
        )

        browser_page.reload(wait_until="domcontentloaded")
        expect(browser_page.locator("#ptb-board-width")).to_have_value(
            "10", timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator("#ptb-preview-status")).to_contain_text(
            _OVERFLOW_MESSAGE, timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator(".ptb-overflow-layer")).to_have_count(1)
        expect(browser_page.locator("#ptb-preview-summary")).to_have_text(
            "64パッド + purge pad"
        )
        assert browser_page.locator(".ptb-paste").count() > 0
        expect(browser_page.locator("#ptb-generate")).to_be_disabled()
        expect(browser_page.locator("#ptb-export")).to_be_enabled()

        browser_page.locator("#ptb-board-width").fill("42")
        expect(browser_page.locator("#ptb-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )
        browser_page.locator("#ptb-custom-pad-shape").select_option("oval")
        browser_page.locator("#ptb-custom-pad-width").fill("1.5")
        browser_page.locator("#ptb-custom-pad-height").fill("0.5")
        browser_page.locator("#ptb-add-custom-pad").click()
        expect(browser_page.locator("#ptb-pattern-rows tr")).to_have_count(
            7, timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator("#ptb-preview-summary")).to_have_text(
            "76パッド + purge pad", timeout=_BROWSER_TIMEOUT_MS
        )

        browser_page.reload(wait_until="domcontentloaded")
        expect(browser_page.locator("#ptb-board-width")).to_have_value(
            "42", timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator("#ptb-pattern-rows tr")).to_have_count(
            7, timeout=_BROWSER_TIMEOUT_MS
        )
        expect(
            browser_page.locator("#ptb-pattern-rows tr").filter(
                has_text="長円（スロット） 1.5 × 0.5 mm"
            )
        ).to_have_count(1)
        expect(browser_page.locator("#ptb-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )

    def test_sorting_does_not_change_the_canonical_export_order(
        self, live_ui: LiveUi, browser_page, tmp_path: Path
    ):
        _open_board_generator(browser_page, live_ui)
        row_ids = browser_page.locator("#ptb-pattern-rows tr")
        canonical = row_ids.evaluate_all(
            "elements => elements.map(element => element.dataset.catalogId)"
        )

        name_sort = browser_page.locator('[data-sort-field="name"]')
        name_sort.click()
        name_sort.click()
        sorted_ids = row_ids.evaluate_all(
            "elements => elements.map(element => element.dataset.catalogId)"
        )

        assert sorted_ids != canonical
        with browser_page.expect_download(timeout=_BROWSER_TIMEOUT_MS) as info:
            browser_page.locator("#ptb-export").click()
        output = tmp_path / "sorted-export.json"
        info.value.save_as(output)
        document = json.loads(output.read_text(encoding="utf-8"))
        assert [item["catalog_id"] for item in document["patterns"]] == canonical

    def test_preview_response_keeps_focus_in_the_edited_pattern_field(
        self, live_ui: LiveUi, browser_page
    ):
        _open_board_generator(browser_page, live_ui)
        rotation_count = browser_page.locator(
            '#ptb-pattern-rows tr:first-child [data-pattern-field="rotation_count"]'
        )

        rotation_count.click()
        rotation_count.press("Control+A")
        rotation_count.type("2", delay=50)
        expect(
            browser_page.locator(
                "#ptb-pattern-rows tr:first-child .ptb-resolved-angles"
            )
        ).to_have_text("0°, 90°", timeout=_BROWSER_TIMEOUT_MS)

        assert rotation_count.evaluate("element => document.activeElement === element")

    @pytest.mark.parametrize(
        "stored",
        [
            json.dumps({"kind": "wrong-kind", "schema_version": 1}),
            "{malformed-json",
        ],
    )
    def test_invalid_saved_draft_is_replaced_by_defaults(
        self,
        live_ui: LiveUi,
        browser_page,
        stored: str,
    ):
        _open_board_generator(browser_page, live_ui)
        storage_key = browser_page.evaluate(
            "() => 'pcbasm:paste-test-board:draft:' + "
            "(document.body.dataset.machineBase || 'unscoped')"
        )
        browser_page.evaluate(
            "entry => localStorage.setItem(entry.key, entry.value)",
            {
                "key": storage_key,
                "value": stored,
            },
        )

        browser_page.reload(wait_until="domcontentloaded")

        expect(browser_page.locator("#ptb-board-width")).to_have_value(
            "40", timeout=_BROWSER_TIMEOUT_MS
        )
        restored = json.loads(
            browser_page.evaluate("key => localStorage.getItem(key)", storage_key)
        )
        assert restored["kind"] == "paste_test_board"
        assert restored["schema_version"] == 1
        assert restored["board"]["width_mm"] == 40.0

    def test_transient_draft_restore_failure_locks_editing_and_keeps_the_draft(
        self, live_ui: LiveUi, browser_page
    ):
        _open_board_generator(browser_page, live_ui)
        storage_key = browser_page.evaluate(
            "() => 'pcbasm:paste-test-board:draft:' + "
            "(document.body.dataset.machineBase || 'unscoped')"
        )
        saved = json.loads(
            browser_page.evaluate("key => localStorage.getItem(key)", storage_key)
        )
        saved["patterns"][0]["catalog_id"] = "Missing.pretty/Foo#pad-0"
        serialized = json.dumps(saved)
        browser_page.evaluate(
            "entry => localStorage.setItem(entry.key, entry.value)",
            {"key": storage_key, "value": serialized},
        )
        browser_page.reload(wait_until="domcontentloaded")

        expect(browser_page.locator("#ptb-preview-status")).to_contain_text(
            "保存していた設定の復元に失敗", timeout=_BROWSER_TIMEOUT_MS
        )
        assert browser_page.locator(".ptb-interactive").evaluate(
            "element => element.disabled"
        )
        assert browser_page.locator(".ptb-actions").evaluate(
            "element => element.disabled"
        )
        assert browser_page.locator("#ptb-preview-status").evaluate(
            "element => element.closest('fieldset[disabled]') === null"
        )
        assert (
            browser_page.evaluate("key => localStorage.getItem(key)", storage_key)
            == serialized
        )

    def test_export_import_and_kicad_download_need_no_control(
        self, live_ui: LiveUi, browser_page, tmp_path: Path
    ):
        _open_board_generator(browser_page, live_ui)
        assert browser_page.locator("body").get_attribute("data-control") == "free"
        assert browser_page.locator("#ptb-generate").evaluate(
            "element => !element.closest('fieldset').disabled"
        )

        with browser_page.expect_download(timeout=_BROWSER_TIMEOUT_MS) as export_info:
            browser_page.locator("#ptb-export").click()
        exported = export_info.value
        assert exported.suggested_filename == ("pcbasm-paste-test-board.json")
        config_path = tmp_path / exported.suggested_filename
        exported.save_as(config_path)
        document = json.loads(config_path.read_text(encoding="utf-8"))
        assert document["kind"] == "paste_test_board"
        assert document["schema_version"] == 1
        assert "auto_pack" not in document
        assert document["custom_pads"] == []
        assert all("transpose" not in pattern for pattern in document["patterns"])

        browser_page.locator("#ptb-board-width").fill("42")
        expect(browser_page.locator("#ptb-board-width")).to_have_value(
            "42", timeout=_BROWSER_TIMEOUT_MS
        )
        browser_page.locator("#ptb-import-file").set_input_files(config_path)
        expect(browser_page.locator("#ptb-board-width")).to_have_value(
            "40", timeout=_BROWSER_TIMEOUT_MS
        )
        expect(browser_page.locator("#ptb-preview-status")).to_have_text(
            "配置可能です", timeout=_BROWSER_TIMEOUT_MS
        )

        with browser_page.expect_download(timeout=_BROWSER_TIMEOUT_MS) as board_info:
            browser_page.locator("#ptb-generate").click()
        downloaded = board_info.value
        assert downloaded.suggested_filename == ("pcbasm-paste-test-board.kicad_pcb")
        board_path = tmp_path / downloaded.suggested_filename
        downloaded.save_as(board_path)
        pcb = PcbFile(board_path)
        assert pcb.outline.width == 40.0
        assert len(pcb.components) == 65
        assert len(pcb.pads) == 65
