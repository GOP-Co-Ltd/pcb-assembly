"""PCB ファイルを実ブラウザのキーボード操作・アップロードで選択する。"""

import shutil

import httpx
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import LiveServer, LiveUi, acquire_control
from tests.web.api.conftest import COPPER_PCB_FIXTURE


def _open_file_browser(live_ui: LiveUi, page):
    page.goto(f"{live_ui.base_url}/pasting/loading")
    acquire_control(page)
    menu = page.get_by_test_id("topbar-menu")
    if menu.get_attribute("open") is None:
        menu.locator("summary").click()
    chip = page.get_by_test_id("pcb-chip")
    chip.focus()
    chip.press("Enter")
    dialog = page.get_by_role("dialog", name="PCBファイルを選択")
    expect(dialog).to_be_visible()
    return dialog


def _selected_pcb(live_server: LiveServer) -> str | None:
    response = httpx.get(f"{live_server.base_url}/api/state", timeout=10.0)
    assert response.status_code == 200, response.text
    return response.json()["pcb_file"]


class TestPcbFileBrowser:
    @pytest.mark.parametrize("width", [1280, 390, 320])
    def test_keyboard_navigation_selects_a_long_filename_without_horizontal_scroll(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page, width: int
    ):
        directory = live_server.settings.pcb_browse_root / "試作基板"
        directory.mkdir()
        filename = "assembly_board_revision_" * 5 + ".kicad_pcb"
        shutil.copyfile(COPPER_PCB_FIXTURE, directory / filename)
        browser_page.set_viewport_size({"width": width, "height": 844})
        dialog = _open_file_browser(live_ui, browser_page)
        folder = dialog.get_by_role("button", name="試作基板")
        expect(folder).to_be_visible()
        close = dialog.get_by_role("button", name="閉じる", exact=True)
        close.focus()
        close.press("Tab")
        expect(folder).to_be_focused()
        folder.press("Enter")

        parent = dialog.get_by_role("button", name="上のフォルダー")
        expect(parent).to_be_visible()
        close.focus()
        close.press("Tab")
        expect(parent).to_be_focused()
        parent.press("Enter")
        expect(folder).to_be_visible()
        close.focus()
        close.press("Tab")
        folder.press("Enter")

        file = dialog.get_by_role("button", name=filename)
        expect(file).to_be_visible()
        assert dialog.locator("#fb-entries").evaluate(
            "el => el.scrollWidth <= el.clientWidth"
        )
        assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
        assert file.bounding_box()["height"] >= 44
        close.focus()
        close.press("Tab")
        parent.press("Tab")
        expect(file).to_be_focused()
        file.press("Enter")

        expect(dialog).to_be_hidden()
        assert _selected_pcb(live_server) == f"試作基板/{filename}"

    def test_missing_file_error_is_shown_in_the_dialog_and_selection_can_be_retried(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        pcb = live_server.settings.pcb_browse_root / "board.kicad_pcb"
        shutil.copyfile(COPPER_PCB_FIXTURE, pcb)
        dialog = _open_file_browser(live_ui, browser_page)
        file = dialog.get_by_role("button", name=pcb.name)
        expect(file).to_be_visible()
        pcb.unlink()
        file.press("Enter")
        expect(dialog.get_by_role("alert")).to_contain_text("ファイルが存在しません")
        expect(dialog).to_be_visible()
        assert _selected_pcb(live_server) is None

        shutil.copyfile(COPPER_PCB_FIXTURE, pcb)
        file.press("Enter")
        expect(dialog).to_be_hidden()
        assert _selected_pcb(live_server) == pcb.name

    def test_empty_directory_and_upload_error_allow_a_successful_retry(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        dialog = _open_file_browser(live_ui, browser_page)
        expect(dialog.get_by_role("status")).to_contain_text(
            "選択できる PCB ファイルやフォルダーがありません"
        )
        upload = dialog.locator('input[type="file"]')
        upload.set_input_files(
            {"name": "invalid.txt", "mimeType": "text/plain", "buffer": b"not a PCB"}
        )
        expect(dialog.get_by_role("alert")).to_contain_text(
            ".kicad_pcb ファイルをアップロードしてください"
        )
        assert _selected_pcb(live_server) is None
        assert not live_server.settings.pcb_upload_dir.exists()

        upload.set_input_files(COPPER_PCB_FIXTURE)
        expect(dialog).to_be_hidden()
        assert _selected_pcb(live_server) == f"uploads/{COPPER_PCB_FIXTURE.name}"
        saved = live_server.settings.pcb_upload_dir / COPPER_PCB_FIXTURE.name
        assert saved.read_bytes() == COPPER_PCB_FIXTURE.read_bytes()
