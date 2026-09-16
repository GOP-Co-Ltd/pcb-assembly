"""パッド設定の変更後、遅れて届く順路・塗布パスを表示しない。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    LiveServer,
    LiveUi,
    acquire_control,
    get_pad_config,
    select_led_blinker,
)
from tests.e2e.network import DelayedHttp, delayed_ui


@pytest.fixture
def delayed_preview_ui(
    live_server: LiveServer, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[tuple[LiveUi, DelayedHttp, str]]:
    kind = request.param
    with delayed_ui(
        live_server,
        tmp_path,
        method="POST",
        path_suffixes=(f"/api/pasting/pad-config/{kind}",),
        response=True,
    ) as (ui, transport):
        yield ui, transport, kind


class TestPadPreviews:
    @pytest.mark.parametrize(
        "delayed_preview_ui", ["route", "fill-path"], indirect=True
    )
    def test_a_changed_selection_discards_pending_results_and_can_be_recalculated(
        self, live_server: LiveServer, delayed_preview_ui, browser_page
    ):
        ui, transport, kind = delayed_preview_ui
        page = browser_page
        select_led_blinker(live_server)
        page.goto(f"{ui.base_url}/pasting/paste_solder", wait_until="domcontentloaded")
        acquire_control(page)
        calculate = page.locator(f"#pad-calculate-{kind}")
        overlay = page.get_by_test_id(f"pad-{kind}-overlay")
        calculate.click()
        assert transport.wait_received()

        with page.expect_response("**/api/pasting/pad-config"):
            page.locator("#pad-disable-all").click()
        assert not any(
            pad["enabled"]
            for pad in get_pad_config(live_server)["pads"]
            if pad["layer"] == "Top"
        )

        with page.expect_response(f"**/api/pasting/pad-config/{kind}"):
            transport.resume()
        page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        expect(overlay).to_have_count(0)
        expect(calculate).to_be_enabled()

        with page.expect_response("**/api/pasting/pad-config"):
            page.locator("#pad-enable-all").click()
        expect(page.locator('.pad-disabled[data-layer="Top"]')).to_have_count(0)
        calculate.click()
        expect(overlay).to_be_attached()
