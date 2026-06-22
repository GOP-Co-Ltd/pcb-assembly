"""paste_solder pad editor の実ブラウザ E2E 仕様テスト.

実 uvicorn (`live_server`) と Playwright sync API で、buildless UI の観測可能な
振る舞いだけを検証する。基板は実 fixture の led_blinker を隔離 pcb root へ
コピーし、公開 API で選択する。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from tests.e2e.conftest import LiveServer
from tests.webui.conftest import COPPER_PCB_FIXTURE

_HTTP_TIMEOUT = 10.0
_BROWSER_TIMEOUT_MS = 10_000
_POLL_TIMEOUT = 5.0
_DESCENDANT_OVERRIDE_MARKER_SELECTOR = ",".join(
    [
        '[data-testid="pad-descendant-override-marker"]',
        ".pad-descendant-override-marker",
        ".pad-descendant-override-badge",
    ]
)
_DESCENDANT_FIELD_MARKER_SELECTOR = ",".join(
    [
        '[data-testid="pad-descendant-field-marker"]',
        ".pad-descendant-field-marker",
        ".pad-descendant-override-field-marker",
    ]
)
_DESCENDANT_WARNING_RE = re.compile(
    r"下位|配下|子孫|個別|override|上書き|継承", re.IGNORECASE
)
_HIGHLIGHT_SELECTOR = ",".join(
    [
        '[data-testid="pad-polygon"].pad-node-highlight',
        '[data-testid="pad-polygon"][data-highlighted="true"]',
        '[data-testid="pad-polygon"][aria-selected="true"]',
    ]
)
_VIEWPORTS = (
    ("desktop", 1280, 900),
    ("tablet", 834, 1112),
    ("mobile", 390, 844),
)


def _css_string(value: str) -> str:
    return json.dumps(value)


def _testid(value: str) -> str:
    return f"[data-testid={_css_string(value)}]"


def _pad_selector(pad_id: str) -> str:
    return f'{_testid("pad-polygon")}[data-pad-id={_css_string(pad_id)}]'


def _row_selector(node_id: str) -> str:
    return f'{_testid("pad-tree-row")}[data-node-id={_css_string(node_id)}]'


def _select_led_blinker(live_server: LiveServer):
    destination = live_server.settings.pcb_browse_root / "led_blinker"
    shutil.copytree(COPPER_PCB_FIXTURE.parent, destination)
    response = httpx.put(
        f"{live_server.base_url}/api/pcb-file",
        json={"path": "led_blinker/led_blinker.kicad_pcb"},
        timeout=_HTTP_TIMEOUT,
    )
    assert response.status_code == 200, response.text


def _get_pad_config(live_server: LiveServer) -> dict[str, Any]:
    response = httpx.get(
        f"{live_server.base_url}/api/pasting/pad-config",
        timeout=_HTTP_TIMEOUT,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _calculate_route(live_server: LiveServer, layer: str = "Top") -> dict[str, Any]:
    response = httpx.post(
        f"{live_server.base_url}/api/pasting/pad-config/route",
        json={"layer": layer},
        timeout=_HTTP_TIMEOUT,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _calculate_fill_path(live_server: LiveServer, layer: str = "Top") -> dict[str, Any]:
    response = httpx.post(
        f"{live_server.base_url}/api/pasting/pad-config/fill-path",
        json={"layer": layer},
        timeout=_HTTP_TIMEOUT,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _patch_pad_config_node(
    live_server: LiveServer, node_id: str, values: dict[str, float]
):
    response = httpx.patch(
        f"{live_server.base_url}/api/pasting/pad-config/node",
        json={"node": node_id, "values": values},
        timeout=_HTTP_TIMEOUT,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _open_paste_solder(page: Any, live_server: LiveServer):
    page.goto(
        f"{live_server.base_url}/pasting/paste_solder",
        wait_until="domcontentloaded",
    )
    page.locator(_testid("pad-viewer")).wait_for(
        state="visible", timeout=_BROWSER_TIMEOUT_MS
    )
    page.wait_for_function(
        """() => document.querySelectorAll('[data-testid="pad-polygon"]').length > 0""",
        timeout=_BROWSER_TIMEOUT_MS,
    )


def _find_pad(config: dict[str, Any], pad_id: str) -> dict[str, Any]:
    return next(pad for pad in config["pads"] if pad["id"] == pad_id)


def _tree_path_ids(node: dict[str, Any], target_id: str) -> list[str]:
    if node["id"] == target_id:
        return [node["id"]]
    for child in node["children"]:
        child_path = _tree_path_ids(child, target_id)
        if child_path:
            return [node["id"], *child_path]
    return []


def _l3_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [node] if node["level"] == 3 else []
    for child in node["children"]:
        nodes.extend(_l3_nodes(child))
    return nodes


def _descendant_l4_pad_ids(node: dict[str, Any]) -> set[str]:
    if node["level"] == 4:
        _, designator, pad_number = node["id"].split(":", 2)
        return {f"{designator}.{pad_number}"}
    ids: set[str] = set()
    for child in node["children"]:
        ids |= _descendant_l4_pad_ids(child)
    return ids


def _choose_l3_node_id(config: dict[str, Any]) -> str:
    all_pad_ids = {pad["id"] for pad in config["pads"]}
    for node in _l3_nodes(config["tree"]):
        pad_ids = _descendant_l4_pad_ids(node)
        members = [pad for pad in config["pads"] if pad["id"] in pad_ids]
        if members and any(pad["layer"] == "Top" for pad in members):
            if pad_ids != all_pad_ids:
                return node["id"]
    raise AssertionError("Top pad を含む L3 node が見つからない")


def _ensure_row_visible(page: Any, path_ids: list[str]) -> Any:
    for index, node_id in enumerate(path_ids):
        row = page.locator(_row_selector(node_id))
        row.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        if index == len(path_ids) - 1:
            row.scroll_into_view_if_needed(timeout=_BROWSER_TIMEOUT_MS)
            return row

        next_row = page.locator(_row_selector(path_ids[index + 1]))
        if next_row.count() == 0 or not next_row.nth(0).is_visible():
            toggle = row.locator(f'{_testid("pad-tree-toggle")}, button')
            if toggle.count() > 0:
                toggle.nth(0).click()
            else:
                row.click()
            next_row.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
    raise AssertionError("tree row が見つからない")


def _ensure_row_collapsed(page: Any, node_id: str, descendant_id: str):
    row = page.locator(_row_selector(node_id))
    row.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
    descendant = page.locator(_row_selector(descendant_id))
    if descendant.count() > 0 and descendant.nth(0).is_visible():
        row.locator(_testid("pad-tree-toggle")).nth(0).click()
    descendant.wait_for(state="hidden", timeout=_BROWSER_TIMEOUT_MS)


def _field_cell(row: Any, field: str) -> Any:
    return row.locator(f"td:has(input[data-field={_css_string(field)}])")


def _field_input(row: Any, field: str) -> Any:
    return row.locator(
        f'input[data-testid="pad-setting-input"][data-field={_css_string(field)}]'
    )


def _highlighted_pad_ids(page: Any) -> set[str]:
    ids = page.eval_on_selector_all(
        _HIGHLIGHT_SELECTOR,
        """(els) => els
            .map((el) => el.dataset.padId || el.getAttribute("data-pad-id"))
            .filter(Boolean)""",
    )
    return set(ids)


def _wait_for_highlighted(page: Any, expected_ids: set[str]):
    page.wait_for_function(
        """([selector, expected]) => {
            const actual = [...new Set(
                Array.from(document.querySelectorAll(selector))
                    .map((el) => el.dataset.padId || el.getAttribute("data-pad-id"))
                    .filter(Boolean)
            )].sort();
            const wanted = [...expected].sort();
            return actual.length === wanted.length
                && actual.every((id, index) => id === wanted[index]);
        }""",
        arg=[_HIGHLIGHT_SELECTOR, sorted(expected_ids)],
        timeout=_BROWSER_TIMEOUT_MS,
    )
    assert _highlighted_pad_ids(page) == expected_ids


def _wait_for_pad_enabled(live_server: LiveServer, pad_id: str, enabled: bool):
    deadline = time.monotonic() + _POLL_TIMEOUT
    while True:
        config = _get_pad_config(live_server)
        if _find_pad(config, pad_id)["enabled"] is enabled:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{pad_id} enabled が {enabled} に更新されない")
        time.sleep(0.05)


def _wait_for_node_override_field(
    live_server: LiveServer, node_id: str, field: str, expected: float
) -> dict[str, Any]:
    deadline = time.monotonic() + _POLL_TIMEOUT
    while True:
        config = _get_pad_config(live_server)
        value = config["overrides"].get(node_id, {}).get("values", {}).get(field)
        if value is not None and abs(float(value) - expected) < 1e-9:
            return config
        if time.monotonic() > deadline:
            raise AssertionError(
                f"{node_id} {field} override が {expected} に更新されない"
            )
        time.sleep(0.05)


def _wait_for_node_enabled_override(
    live_server: LiveServer, node_id: str, expected: bool
):
    deadline = time.monotonic() + _POLL_TIMEOUT
    while True:
        config = _get_pad_config(live_server)
        value = config["overrides"].get(node_id, {}).get("enabled")
        if value is expected:
            return
        if time.monotonic() > deadline:
            raise AssertionError(
                f"{node_id} enabled override が {expected} に更新されない"
            )
        time.sleep(0.05)


def _wait_for_node_resolved_field(
    live_server: LiveServer, node_id: str, field: str, expected: float
):
    deadline = time.monotonic() + _POLL_TIMEOUT
    while True:
        config = _get_pad_config(live_server)
        values = [
            pad["resolved"][field]
            for pad in config["pads"]
            if node_id in pad["node_ids"]
        ]
        if values and all(abs(float(value) - expected) < 1e-9 for value in values):
            return
        if time.monotonic() > deadline:
            raise AssertionError(
                f"{node_id} 配下 pad の {field} が "
                f"{expected} に解決されない: {values}"
            )
        time.sleep(0.05)


def _wait_for_layer_enabled(live_server: LiveServer, layer: str, enabled: bool):
    deadline = time.monotonic() + _POLL_TIMEOUT
    while True:
        config = _get_pad_config(live_server)
        layer_pads = [pad for pad in config["pads"] if pad["layer"] == layer]
        if layer_pads and all(pad["enabled"] is enabled for pad in layer_pads):
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{layer} pad enabled が {enabled} に揃わない")
        time.sleep(0.05)


def _assert_in_viewport(page: Any, locator: Any):
    box = locator.bounding_box(timeout=_BROWSER_TIMEOUT_MS)
    assert box is not None
    assert box["width"] > 0
    assert box["height"] > 0
    viewport = page.viewport_size
    assert viewport is not None
    assert box["x"] >= -1
    assert box["y"] >= -1
    assert box["x"] + box["width"] <= viewport["width"] + 1
    assert box["y"] + box["height"] <= viewport["height"] + 1


class TestPasteSolderBrowserRendering:
    """paste_solder の pad SVG と主要 DOM hook."""

    def test_pad_svg_contains_visible_polygons(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        _open_paste_solder(browser_page, live_server)

        for testid in (
            "pad-layer-top",
            "pad-layer-bottom",
            "preview-img",
            "job-run",
        ):
            browser_page.locator(_testid(testid)).wait_for(
                state="attached", timeout=_BROWSER_TIMEOUT_MS
            )

        svg = browser_page.locator(_testid("pad-viewer"))
        assert svg.evaluate("(el) => el.tagName.toLowerCase()") == "svg"
        point_counts = browser_page.eval_on_selector_all(
            _testid("pad-polygon"),
            """(els) => els.map((el) =>
                (el.getAttribute("points") || "")
                    .trim()
                    .split(/\\s+/)
                    .filter(Boolean)
                    .length
            )""",
        )
        assert point_counts
        assert all(count >= 3 for count in point_counts)

        visible_pad = browser_page.locator(f'{_testid("pad-polygon")}:visible').nth(0)
        visible_pad.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        _assert_in_viewport(browser_page, visible_pad)

        config = _get_pad_config(live_server)
        axis_labels = browser_page.eval_on_selector_all(
            _testid("pad-axis-label"),
            "(els) => els.map((el) => el.textContent)",
        )
        assert f"{config['width']:g} mm" in axis_labels
        assert f"{config['height']:g} mm" in axis_labels

        first_top = next(pad for pad in config["pads"] if pad["layer"] == "Top")
        title = browser_page.locator(_pad_selector(first_top["id"])).locator("title")
        assert first_top["designator"] in title.text_content(
            timeout=_BROWSER_TIMEOUT_MS
        )

        root_row = browser_page.locator(_row_selector("L0"))
        first_setting = root_row.locator(_testid("pad-setting-input")).nth(0)
        assert first_setting.input_value(timeout=_BROWSER_TIMEOUT_MS) == ""
        assert first_setting.get_attribute("placeholder") == str(
            config["defaults"]["ul_per_mm2"]
        )
        assert root_row.locator(".pad-override-marker").count() == 0

    def test_fake_camera_preview_image_loads(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        _open_paste_solder(browser_page, live_server)

        preview = browser_page.locator(_testid("preview-img"))
        preview.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        browser_page.wait_for_function(
            """(img) => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0""",
            arg=preview.element_handle(),
            timeout=_BROWSER_TIMEOUT_MS,
        )


class TestPasteSolderBrowserPadInteraction:
    """実ブラウザ操作と API 永続化."""

    def test_pad_click_selects_single_pad_without_toggling_enabled(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        config = _get_pad_config(live_server)
        pad = next(pad for pad in config["pads"] if pad["layer"] == "Top")

        _open_paste_solder(browser_page, live_server)
        polygon = browser_page.locator(_pad_selector(pad["id"]))
        polygon.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)

        polygon.click()
        _wait_for_pad_enabled(live_server, pad["id"], pad["enabled"])
        browser_page.wait_for_function(
            """(selector) =>
                document.querySelector(selector)?.textContent === "選択: 1" """,
            arg=_testid("pad-selection-count"),
            timeout=_BROWSER_TIMEOUT_MS,
        )
        assert "pad-selected" in polygon.get_attribute("class")
        row = browser_page.locator(_row_selector(pad["node_ids"][-1]))
        row.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        assert "pad-row-focus" in row.get_attribute("class")

    def test_layer_switch_and_bulk_enable_disable_persist(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        config = _get_pad_config(live_server)
        bottom_pad = next(pad for pad in config["pads"] if pad["layer"] == "Bottom")

        _open_paste_solder(browser_page, live_server)
        browser_page.locator(_testid("pad-layer-bottom")).check()
        bottom_polygon = browser_page.locator(_pad_selector(bottom_pad["id"]))
        bottom_polygon.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)

        browser_page.locator(_testid("pad-disable-all")).click()
        _wait_for_layer_enabled(live_server, "Bottom", False)

        browser_page.locator(_testid("pad-enable-all")).click()
        _wait_for_layer_enabled(live_server, "Bottom", True)

    def test_tree_row_highlights_pads_by_node_id_membership(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        config = _get_pad_config(live_server)
        node_id = _choose_l3_node_id(config)
        expected_ids = {
            pad["id"] for pad in config["pads"] if node_id in pad["node_ids"]
        }
        assert expected_ids
        assert expected_ids != {pad["id"] for pad in config["pads"]}

        _open_paste_solder(browser_page, live_server)
        path_ids = _tree_path_ids(config["tree"], node_id)
        assert path_ids
        row = _ensure_row_visible(browser_page, path_ids)

        row.hover()
        _wait_for_highlighted(browser_page, expected_ids)
        browser_page.mouse.move(1, 1)
        _wait_for_highlighted(browser_page, set())

        row.click()
        browser_page.mouse.move(1, 1)
        _wait_for_highlighted(browser_page, expected_ids)

    def test_route_button_draws_route_and_enabled_change_clears_it(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        route = _calculate_route(live_server)
        assert len(route["pads"]) > 1
        first = route["pads"][0]

        _open_paste_solder(browser_page, live_server)
        browser_page.locator(_testid("pad-calculate-route")).click()
        browser_page.locator(_testid("pad-route-overlay")).wait_for(
            state="attached", timeout=_BROWSER_TIMEOUT_MS
        )
        browser_page.locator(_testid("pad-route-start")).wait_for(
            state="attached", timeout=_BROWSER_TIMEOUT_MS
        )
        browser_page.locator(_testid("pad-route-end")).wait_for(
            state="attached", timeout=_BROWSER_TIMEOUT_MS
        )
        browser_page.wait_for_function(
            """(selector) => document.querySelectorAll(selector).length > 0""",
            arg=_testid("pad-route-segment"),
            timeout=_BROWSER_TIMEOUT_MS,
        )
        route_colors = browser_page.eval_on_selector_all(
            _testid("pad-route-segment"),
            "(els) => els.map((el) => getComputedStyle(el).stroke)",
        )
        assert len(set(route_colors)) > 1
        arrow_colors = browser_page.eval_on_selector_all(
            ".pad-route-arrow-head",
            "(els) => els.map((el) => getComputedStyle(el).fill)",
        )
        assert arrow_colors == route_colors
        assert browser_page.locator(_testid("pad-route-status")).count() == 0

        first_pad = browser_page.locator(_pad_selector(first["id"]))
        assert first_pad.get_attribute("data-route-order") == "1"
        assert "塗布順 1" in first_pad.locator("title").text_content(
            timeout=_BROWSER_TIMEOUT_MS
        )

        first_pad.click()
        browser_page.locator(_testid("pad-disable-selected")).click()
        browser_page.locator(_testid("pad-route-overlay")).wait_for(
            state="detached", timeout=_BROWSER_TIMEOUT_MS
        )
        assert browser_page.locator(_testid("pad-route-status")).count() == 0

    def test_fill_path_button_draws_paths_and_changes_clear_it(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        fill_path = _calculate_fill_path(live_server)
        assert fill_path["pads"]
        first = fill_path["pads"][0]

        _open_paste_solder(browser_page, live_server)
        browser_page.locator(_testid("pad-calculate-fill-path")).click()
        browser_page.locator(_testid("pad-fill-path-overlay")).wait_for(
            state="attached", timeout=_BROWSER_TIMEOUT_MS
        )
        browser_page.wait_for_function(
            """([lineSelector, pointSelector, directionSelector]) =>
                document.querySelectorAll(lineSelector).length > 0
                && document.querySelectorAll(pointSelector).length > 0
                && document.querySelectorAll(directionSelector).length > 0""",
            arg=[
                _testid("pad-fill-path-polyline"),
                _testid("pad-fill-path-point"),
                _testid("pad-fill-path-direction"),
            ],
            timeout=_BROWSER_TIMEOUT_MS,
        )
        fill_path_colors = browser_page.eval_on_selector_all(
            _testid("pad-fill-path-polyline"),
            "(els) => els.map((el) => getComputedStyle(el).stroke)",
        )
        direction_colors = browser_page.eval_on_selector_all(
            _testid("pad-fill-path-direction"),
            "(els) => els.map((el) => getComputedStyle(el).stroke)",
        )
        arrow_colors = browser_page.eval_on_selector_all(
            ".pad-fill-path-arrow-head",
            "(els) => els.map((el) => getComputedStyle(el).fill)",
        )
        assert set(fill_path_colors) == {"rgb(255, 51, 51)"}
        assert set(direction_colors) == {"rgb(255, 51, 51)"}
        assert set(arrow_colors) == {"rgb(255, 51, 51)"}

        root_row = browser_page.locator(_row_selector("L0"))
        boundary_margin = _field_input(root_row, "boundary_margin")
        boundary_margin.fill("0.3")
        boundary_margin.press("Enter")
        _wait_for_node_override_field(live_server, "L0", "boundary_margin", 0.3)
        browser_page.locator(_testid("pad-fill-path-overlay")).wait_for(
            state="detached", timeout=_BROWSER_TIMEOUT_MS
        )

        browser_page.locator(_testid("pad-calculate-fill-path")).click()
        browser_page.locator(_testid("pad-fill-path-overlay")).wait_for(
            state="attached", timeout=_BROWSER_TIMEOUT_MS
        )
        first_pad = browser_page.locator(_pad_selector(first["id"]))
        first_pad.click()
        browser_page.locator(_testid("pad-disable-selected")).click()
        browser_page.locator(_testid("pad-fill-path-overlay")).wait_for(
            state="detached", timeout=_BROWSER_TIMEOUT_MS
        )

    def test_active_job_locks_pad_editor(self, live_server: LiveServer, browser_page):
        _select_led_blinker(live_server)
        _open_paste_solder(browser_page, live_server)

        start = httpx.post(
            f"{live_server.base_url}/api/jobs/job_demo",
            json={"params": {"steps": 1, "interval": 0.1}},
            timeout=_HTTP_TIMEOUT,
        )
        assert start.status_code == 201, start.text

        browser_page.locator(".pad-editor-locked").wait_for(
            state="attached", timeout=_BROWSER_TIMEOUT_MS
        )
        assert browser_page.locator(_testid("pad-disable-all")).is_disabled()
        assert browser_page.locator(_testid("pad-setting-input")).nth(0).is_disabled()

    def test_saved_override_file_can_be_imported(
        self, live_server: LiveServer, browser_page, tmp_path: Path
    ):
        _select_led_blinker(live_server)
        patch = httpx.patch(
            f"{live_server.base_url}/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"fill_speed": 0.33}},
            timeout=_HTTP_TIMEOUT,
        )
        assert patch.status_code == 200, patch.text
        exported = httpx.get(
            f"{live_server.base_url}/api/pasting/pad-config/export",
            timeout=_HTTP_TIMEOUT,
        )
        assert exported.status_code == 200, exported.text
        reset = httpx.post(
            f"{live_server.base_url}/api/pasting/pad-config/reset",
            timeout=_HTTP_TIMEOUT,
        )
        assert reset.status_code == 200, reset.text

        import_path = tmp_path / "paste-overrides.json"
        import_path.write_text(json.dumps(exported.json()), encoding="utf-8")
        _open_paste_solder(browser_page, live_server)
        browser_page.locator("#pad-import-config").set_input_files(str(import_path))

        browser_page.wait_for_function(
            """async (baseUrl) => {
                const response = await fetch(`${baseUrl}/api/pasting/pad-config`);
                const config = await response.json();
                return config.overrides["L2:U1"]?.values?.fill_speed === 0.33;
            }""",
            arg=live_server.base_url,
            timeout=_BROWSER_TIMEOUT_MS,
        )


class TestPasteSolderBrowserOverrideVisibility:
    """階層 override の祖先表示と編集時の競合警告."""

    def test_descendant_override_stays_visible_and_specific_after_parent_edit(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        _patch_pad_config_node(live_server, "L2:U1", {"fill_speed": 0.33})
        config = _get_pad_config(live_server)
        path = _tree_path_ids(config["tree"], "L2:U1")
        assert path == ["L0", "L1:SOT-23-6", "L2:U1"]

        _open_paste_solder(browser_page, live_server)
        l1_row = browser_page.locator(_row_selector("L1:SOT-23-6"))
        l1_row.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        _ensure_row_collapsed(browser_page, "L1:SOT-23-6", "L2:U1")

        l1_row.locator(
            f".pad-col-node {_DESCENDANT_OVERRIDE_MARKER_SELECTOR}"
        ).first.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        l1_fill_speed_cell = _field_cell(l1_row, "fill_speed")
        l1_fill_speed_cell.locator(_DESCENDANT_FIELD_MARKER_SELECTOR).first.wait_for(
            state="visible", timeout=_BROWSER_TIMEOUT_MS
        )

        l1_fill_speed = _field_input(l1_row, "fill_speed")
        l1_fill_speed.fill("0.77")
        l1_fill_speed.press("Enter")

        browser_page.locator(".toast").filter(
            has_text=_DESCENDANT_WARNING_RE
        ).first.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        _wait_for_node_override_field(live_server, "L1:SOT-23-6", "fill_speed", 0.77)
        _wait_for_node_resolved_field(live_server, "L2:U1", "fill_speed", 0.33)

        l2_row = _ensure_row_visible(browser_page, ["L0", "L1:SOT-23-6", "L2:U1"])
        l2_fill_speed_cell = _field_cell(l2_row, "fill_speed")
        l2_fill_speed_input = _field_input(l2_row, "fill_speed")
        assert l2_fill_speed_input.input_value(timeout=_BROWSER_TIMEOUT_MS) == "0.33"
        assert "override" in l2_fill_speed_input.get_attribute("class")
        l2_fill_speed_cell.locator(".pad-override-marker").wait_for(
            state="visible", timeout=_BROWSER_TIMEOUT_MS
        )
        l2_fill_speed_cell.locator(".pad-cell-clear").wait_for(
            state="visible", timeout=_BROWSER_TIMEOUT_MS
        )

    def test_bulk_pad_enable_patch_updates_override_visibility_without_reload(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        config = _get_pad_config(live_server)
        target_pad = next(
            pad
            for pad in config["pads"]
            if pad["layer"] == "Top" and "L2:U1" in pad["node_ids"]
        )
        target_l4 = next(
            node_id for node_id in target_pad["node_ids"] if node_id.startswith("L4:")
        )
        target_path = _tree_path_ids(config["tree"], target_l4)
        assert target_path[:3] == ["L0", "L1:SOT-23-6", "L2:U1"]

        _open_paste_solder(browser_page, live_server)
        l1_row = browser_page.locator(_row_selector("L1:SOT-23-6"))
        l1_row.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        _ensure_row_collapsed(browser_page, "L1:SOT-23-6", "L2:U1")

        browser_page.locator(_testid("pad-disable-all")).click()
        _wait_for_node_enabled_override(live_server, target_l4, False)

        l1_row.locator(_DESCENDANT_OVERRIDE_MARKER_SELECTOR).first.wait_for(
            state="visible", timeout=_BROWSER_TIMEOUT_MS
        )
        l4_row = _ensure_row_visible(browser_page, target_path)
        enabled_checkbox = l4_row.locator('.pad-col-enabled input[type="checkbox"]')
        assert not enabled_checkbox.is_checked()
        inherit_button = l4_row.locator(".pad-enabled-inherit")
        inherit_button.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        assert not inherit_button.is_disabled()


class TestPasteSolderBrowserResponsiveLayout:
    """Desktop/tablet/mobile で主要パネルが横方向にはみ出さない."""

    def test_machine_control_collapses_to_handle_width(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        browser_page.set_viewport_size({"width": 1280, "height": 900})
        browser_page.goto(live_server.base_url, wait_until="domcontentloaded")
        browser_page.evaluate("localStorage.removeItem('mc-sidebar-collapsed')")
        _open_paste_solder(browser_page, live_server)

        browser_page.locator("#mc-toggle").click()
        browser_page.wait_for_function(
            """() => {
                const sidebar = document.querySelector('[data-testid="machine-control-sidebar"]');
                return sidebar && sidebar.classList.contains("collapsed")
                    && sidebar.getBoundingClientRect().width <= 40;
            }""",
            timeout=_BROWSER_TIMEOUT_MS,
        )
        assert browser_page.locator(_testid("machine-control")).is_hidden()

    def test_key_panels_do_not_create_horizontal_overflow(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)

        for name, width, height in _VIEWPORTS:
            browser_page.set_viewport_size({"width": width, "height": height})
            _open_paste_solder(browser_page, live_server)
            overflow = browser_page.evaluate(
                """(testids) => {
                    const doc = document.documentElement;
                    const missing = [];
                    const outOfViewport = [];
                    for (const testid of testids) {
                        const el = document.querySelector(`[data-testid="${testid}"]`);
                        if (!el) {
                            missing.push(testid);
                            continue;
                        }
                        const rect = el.getBoundingClientRect();
                        if (rect.left < -1 || rect.right > window.innerWidth + 1) {
                            outOfViewport.push({
                                testid,
                                left: rect.left,
                                right: rect.right,
                                width: window.innerWidth,
                            });
                        }
                    }
                    return {
                        docOverflow: doc.scrollWidth - doc.clientWidth,
                        missing,
                        outOfViewport,
                    };
                }""",
                [
                    "pad-editor-toolbar",
                    "pad-select-tools",
                    "pad-route-tools",
                    "pad-config-tools",
                    "pad-viewer",
                    "preview-img",
                    "job-run",
                ],
            )

            assert overflow["missing"] == [], name
            assert overflow["docOverflow"] <= 1, (name, overflow)
            assert overflow["outOfViewport"] == [], (name, overflow)

    def test_selection_buttons_keep_two_by_two_grid_on_mobile(
        self, live_server: LiveServer, browser_page
    ):
        _select_led_blinker(live_server)
        browser_page.set_viewport_size({"width": 390, "height": 844})
        _open_paste_solder(browser_page, live_server)

        grid = browser_page.evaluate(
            """(selector) => {
                const tools = document.querySelector(selector);
                const buttons = Array.from(tools.querySelectorAll("button"));
                const rows = new Set(buttons.map((button) =>
                    Math.round(button.getBoundingClientRect().top)
                ));
                const columns = new Set(buttons.map((button) =>
                    Math.round(button.getBoundingClientRect().left)
                ));
                const rect = tools.getBoundingClientRect();
                return {
                    rowCount: rows.size,
                    columnCount: columns.size,
                    right: rect.right,
                    viewportWidth: window.innerWidth,
                };
            }""",
            _testid("pad-select-tools"),
        )

        assert grid["rowCount"] == 2
        assert grid["columnCount"] == 2
        assert grid["right"] <= grid["viewportWidth"] + 1
