"""ジョブは編集したパッド設定の保存を待ち、保存拒否時には開始しない。"""

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    LiveServer,
    LiveUi,
    acquire_control,
    configure_synthetic_job,
    select_led_blinker,
    start_app,
)
from tests.e2e.network import delayed_ui
from tests.helpers import FakeAudioPlayer
from web.api.app import create_app
from web.api.jobs.context import JobContext
from web.api.settings import Settings


@pytest.fixture
def live_server(e2e_settings: Settings) -> Iterator[LiveServer]:
    app = create_app(e2e_settings, audio_player=FakeAudioPlayer())

    def run(ctx: JobContext) -> None:
        assert ctx.board_store is not None and ctx.source_pcb is not None
        model = ctx.board_store.load_or_init(
            ctx.source_pcb, ctx.machine.paste_dispenser
        )
        ctx.log(f"started with:{model.levels[0].patch.to_dict()['ul_per_mm2']}")
        ctx.log(f"purge:{ctx.machine.paste_dispenser.initial_purge_ul}")
        ctx.log(f"disabled:{any(level.enabled is False for level in model.levels)}")
        ctx.next_command(timeout=None)

    configure_synthetic_job(app, "paste_solder", run)
    server = start_app(app)
    try:
        live = LiveServer(
            base_url=f"http://127.0.0.1:{server.port}",
            settings=e2e_settings,
            port=server.port,
        )
        select_led_blinker(live)
        httpx.patch(
            f"{live.base_url}/api/pasting/pad-config/node",
            json={"node": "L0", "values": {"ul_per_mm2": 0.15}},
        ).raise_for_status()
        yield live
    finally:
        server.stop()


def _edit(ui: LiveUi, page, value: str):
    page.goto(f"{ui.base_url}/pasting/paste_solder", wait_until="domcontentloaded")
    acquire_control(page)
    field = page.locator('tr[data-node-id="L0"] input[data-field="ul_per_mm2"]')
    expect(field).to_have_value("0.15")
    field.fill(value)
    return field


class TestPadStart:
    @pytest.mark.parametrize("setting", ["amount", "purge"])
    def test_start_flushes_a_debounced_edit(
        self, live_ui: LiveUi, browser_page, setting: str
    ):
        page = browser_page
        _edit(live_ui, page, "0.23" if setting == "amount" else "0.15")
        if setting == "purge":
            page.locator("#pad-initial-purge-amount").fill("0.31")
        page.locator("#job-run").click()
        expected = "started with:0.23" if setting == "amount" else "purge:0.31"
        expect(page.locator("#jc-log")).to_contain_text(expected)

    @pytest.mark.parametrize("value", ["0.23", "-0.23"])
    def test_start_waits_for_inflight_saves_and_recovers_from_rejection(
        self, live_server: LiveServer, tmp_path: Path, browser_page, value: str
    ):
        with delayed_ui(
            live_server,
            tmp_path,
            method="PATCH",
            path_suffixes=("/api/pasting/pad-config/node",),
        ) as (ui, transport):
            page = browser_page
            field = _edit(ui, page, value)
            field.press("Enter")
            assert transport.wait_received()
            page.locator("#job-run").click()
            expect(page.locator("#job-run")).to_be_disabled()
            assert (
                httpx.get(f"{live_server.base_url}/api/jobs/current").json()["job"]
                is None
            )
            transport.resume()
            if value.startswith("-"):
                expect(page.locator("#job-run")).to_be_enabled()
                expect(page.locator("#jc-status")).to_have_text("待機中")
                assert (
                    httpx.get(f"{live_server.base_url}/api/jobs/current").json()["job"]
                    is None
                )
                field.fill("0.23")
                page.locator("#job-run").click()
            expect(page.locator("#jc-log")).to_contain_text("started with:0.23")

    @pytest.mark.parametrize("edit", ["selection", "import"])
    def test_start_waits_for_bulk_edits(
        self, live_server: LiveServer, tmp_path: Path, browser_page, edit: str
    ):
        suffix = "pads" if edit == "selection" else "import"
        with delayed_ui(
            live_server,
            tmp_path,
            method="PATCH" if edit == "selection" else "POST",
            path_suffixes=(f"/api/pasting/pad-config/{suffix}",),
        ) as (ui, transport):
            page = browser_page
            _edit(ui, page, "0.15")
            if edit == "selection":
                page.locator("#pad-disable-all").click()
            else:
                response = httpx.get(
                    f"{live_server.base_url}/api/pasting/pad-config/export"
                )
                response.raise_for_status()
                document = response.json()
                for level in document["settings"]["levels"]:
                    if level["key"] == ["L0"]:
                        level["override"]["ul_per_mm2"] = 0.23
                imported = tmp_path / "settings.json"
                imported.write_text(json.dumps(document))
                page.locator("#pad-import-config").set_input_files(imported)
            assert transport.wait_received()
            page.locator("#job-run").click()
            expect(page.locator("#job-run")).to_be_disabled()
            assert (
                httpx.get(f"{live_server.base_url}/api/jobs/current").json()["job"]
                is None
            )
            transport.resume()
            expected = "disabled:True" if edit == "selection" else "started with:0.23"
            expect(page.locator("#jc-log")).to_contain_text(expected)

    @pytest.mark.parametrize("setting", ["height", "purge"])
    def test_incomplete_numeric_edits_block_start_until_corrected(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page, setting: str
    ):
        page = browser_page
        _edit(live_ui, page, "0.15")
        if setting == "height":
            page.locator(
                'tr[data-node-id="L0"] select[data-field="paste_height"]'
            ).select_option("manual")
            field = page.locator(
                'tr[data-node-id="L0"] input[data-field="paste_height"]'
            )
        else:
            field = page.locator("#pad-initial-purge-amount")
        field.fill("")
        page.locator("#job-run").click()
        expect(page.locator("#toasts")).to_contain_text("入力内容を確認")
        assert (
            httpx.get(f"{live_server.base_url}/api/jobs/current").json()["job"] is None
        )
        expect(field).to_be_focused()
        field.fill("0.31")
        page.locator("#job-run").click()
        expect(page.locator("#jc-log")).to_contain_text("started with:0.15")

    @pytest.mark.parametrize("commit", ["Enter", "Tab"])
    def test_a_rejected_draft_is_retried_before_start(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page, commit: str
    ):
        page = browser_page
        field = _edit(live_ui, page, "-0.23")
        with page.expect_response("**/api/pasting/pad-config/node") as saved:
            field.press(commit)
        assert saved.value.status == 400
        expect(page.locator("#toasts")).to_contain_text("設定更新失敗")
        page.locator("#job-run").click()
        expect(page.locator("#toasts")).to_contain_text("開始しませんでした")
        assert (
            httpx.get(f"{live_server.base_url}/api/jobs/current").json()["job"] is None
        )
        field.fill("0.23")
        page.locator("#job-run").click()
        expect(page.locator("#jc-log")).to_contain_text("started with:0.23")
