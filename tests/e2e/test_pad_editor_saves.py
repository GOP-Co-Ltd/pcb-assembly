"""階層表の遅延保存が、後から選んだ継承設定を上書きしない。"""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect

from tests.e2e.conftest import (
    LiveServer,
    LiveUi,
    acquire_control,
    get_pad_config,
    select_led_blinker,
)
from tests.e2e.network import DelayedHttp, delayed_ui


@pytest.fixture
def saved_pad(live_server: LiveServer) -> None:
    select_led_blinker(live_server)
    response = httpx.patch(
        f"{live_server.base_url}/api/pasting/pad-config/node",
        json={"node": "L0", "values": {"ul_per_mm2": 0.15}},
        timeout=10,
    )
    response.raise_for_status()


@pytest.fixture
def delayed_save_ui(
    live_server: LiveServer, saved_pad: None, tmp_path: Path
) -> Iterator[tuple[LiveUi, DelayedHttp]]:
    with delayed_ui(
        live_server,
        tmp_path,
        method="PATCH",
        path_suffixes=("/api/pasting/pad-config/node",),
    ) as pair:
        yield pair


def _open_editor(ui: LiveUi, page):
    page.goto(f"{ui.base_url}/pasting/paste_solder", wait_until="domcontentloaded")
    acquire_control(page)
    root = page.locator('tr[data-node-id="L0"]')
    expect(root.locator('input[data-field="ul_per_mm2"]')).to_have_value("0.15")
    return root


def _is_clear(request) -> bool:
    return (
        request.method == "PATCH"
        and request.url.endswith("/api/pasting/pad-config/node")
        and '"clear"' in (request.post_data or "")
    )


class TestPadEditorSaves:
    def test_clear_cancels_a_debounced_value_before_the_next_edit(
        self, live_server: LiveServer, live_ui: LiveUi, saved_pad: None, browser_page
    ):
        page = browser_page
        root = _open_editor(live_ui, page)
        root.locator('input[data-field="ul_per_mm2"]').fill("0.23")
        root.locator(
            '[data-testid="pad-clear-override"][data-field="ul_per_mm2"]'
        ).click()
        marker = root.locator(
            '[data-testid="pad-own-override-marker"][data-field="ul_per_mm2"]'
        )
        expect(marker).not_to_be_attached()

        # 次の編集の debounce と保存が完了しても、先の解除が維持される。
        child = page.locator('tr[data-node-id]:not([data-node-id="L0"])').first
        child.locator('input[data-field="ul_per_mm2"]').fill("0.24")
        child.locator('input[data-field="ul_per_mm2"]').press("Enter")
        expect(
            child.locator(
                '[data-testid="pad-own-override-marker"][data-field="ul_per_mm2"]'
            )
        ).to_be_attached()

        assert get_pad_config(live_server)["tree"]["own_override"]["values"] == {}
        expect(marker).not_to_be_attached()
        expect(root.locator('input[data-field="ul_per_mm2"]')).to_have_value("")

    @pytest.mark.parametrize(("value", "status"), [("0.23", 200), ("-0.23", 400)])
    def test_clear_waits_for_an_inflight_save_even_when_that_save_is_rejected(
        self,
        live_server: LiveServer,
        delayed_save_ui,
        browser_page,
        value: str,
        status: int,
    ):
        ui, transport = delayed_save_ui
        page = browser_page
        root = _open_editor(ui, page)
        field = root.locator('input[data-field="ul_per_mm2"]')
        field.fill(value)
        field.press("Enter")
        assert transport.wait_received()

        # 前の要求が止まっている間は解除要求を追い越して送らない。
        with pytest.raises(PlaywrightTimeoutError):
            with page.expect_request(_is_clear, timeout=400):
                root.locator(
                    '[data-testid="pad-clear-override"][data-field="ul_per_mm2"]'
                ).click()

        with (
            page.expect_response(
                lambda response: response.request.method == "PATCH"
                and '"values"' in (response.request.post_data or "")
            ) as earlier,
            page.expect_response(
                lambda response: _is_clear(response.request)
            ) as cleared,
        ):
            transport.resume()
        assert earlier.value.status == status
        assert cleared.value.status == 200
        expect(
            root.locator(
                '[data-testid="pad-own-override-marker"][data-field="ul_per_mm2"]'
            )
        ).not_to_be_attached()
        expect(field).to_have_value("")
        assert get_pad_config(live_server)["tree"]["own_override"]["values"] == {}
