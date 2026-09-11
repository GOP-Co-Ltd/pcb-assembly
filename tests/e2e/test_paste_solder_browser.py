"""paste_solder pad editor の実ブラウザ E2E 仕様テスト.

実 uvicorn（backend `live_server` + frontend `live_ui`）と Playwright sync API で、buildless UI の観測可能な
振る舞いだけを検証する。基板は実 fixture の led_blinker を隔離 pcb root へ
コピーし、公開 API で選択する。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import expect

from tests.e2e.conftest import (
    LiveServer,
    LiveUi,
    acquire_control as _acquire_control,
    get_pad_config as _get_pad_config,
    select_led_blinker as _select_led_blinker,
    session_headers as _session_headers,
    wait_for_config,
)

_HTTP_TIMEOUT = 10.0
_BROWSER_TIMEOUT_MS = 10_000
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
    ("mobile", 390, 844),
)


def _css_string(value: str) -> str:
    return json.dumps(value)


def _testid(value: str) -> str:
    return f"[data-testid={_css_string(value)}]"


def _pad_selector(pad_id: str) -> str:
    return f"{_testid('pad-polygon')}[data-pad-id={_css_string(pad_id)}]"


def _row_selector(node_id: str) -> str:
    return f"{_testid('pad-tree-row')}[data-node-id={_css_string(node_id)}]"


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
    live_server: LiveServer, node_id: str, values: dict[str, float | str]
):
    response = httpx.patch(
        f"{live_server.base_url}/api/pasting/pad-config/node",
        json={"node": node_id, "values": values},
        timeout=_HTTP_TIMEOUT,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _open_pasting_pad_editor(page: Any, live_ui: LiveUi, feature: str):
    page.goto(
        f"{live_ui.base_url}/pasting/{feature}",
        wait_until="domcontentloaded",
    )
    # 変更系（pad 編集・ジョブ開始）は操作権が無いと inert でクリックが届かない
    _acquire_control(page)
    page.locator(_testid("pad-viewer")).wait_for(
        state="visible", timeout=_BROWSER_TIMEOUT_MS
    )
    page.wait_for_function(
        """() => document.querySelectorAll('[data-testid="pad-polygon"]').length > 0""",
        timeout=_BROWSER_TIMEOUT_MS,
    )


def _open_paste_solder(page: Any, live_ui: LiveUi):
    _open_pasting_pad_editor(page, live_ui, "paste_solder")


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
            toggle = row.locator(f"{_testid('pad-tree-toggle')}, button")
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
    return row.locator(f"td:has([data-field={_css_string(field)}])")


def _field_input(row: Any, field: str) -> Any:
    return row.locator(
        f'input[data-testid="pad-setting-input"][data-field={_css_string(field)}]'
    )


def _field_select(row: Any, field: str, testid: str) -> Any:
    return row.locator(
        f"select[data-testid={_css_string(testid)}][data-field={_css_string(field)}]"
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


def _approx(value: object, expected: float) -> bool:
    return isinstance(value, (int, float)) and abs(float(value) - expected) < 1e-9


def _wait_for_override(
    live_server: LiveServer, node_id: str, field: str, expected: float | str
) -> dict[str, Any]:
    """Node override の 1 field が期待値になるまで待つ（float は誤差許容）."""

    def matches(config: dict[str, Any]) -> bool:
        value = config["overrides"].get(node_id, {}).get("values", {}).get(field)
        if isinstance(expected, float):
            return _approx(value, expected)
        return value == expected

    return wait_for_config(
        live_server, matches, f"{node_id} {field} override -> {expected!r}"
    )


def _wait_for_pad_enabled(live_server: LiveServer, pad_id: str, enabled: bool):
    wait_for_config(
        live_server,
        lambda config: _find_pad(config, pad_id)["enabled"] is enabled,
        f"{pad_id} enabled -> {enabled}",
    )


def _wait_for_layer_enabled(live_server: LiveServer, layer: str, enabled: bool):
    def matches(config: dict[str, Any]) -> bool:
        layer_pads = [pad for pad in config["pads"] if pad["layer"] == layer]
        return bool(layer_pads) and all(pad["enabled"] is enabled for pad in layer_pads)

    wait_for_config(live_server, matches, f"{layer} pad enabled -> {enabled}")


def _wait_for_initial_purge(
    live_server: LiveServer,
    *,
    amount: float | None = None,
    resolved_source: str | None = None,
) -> dict[str, Any]:
    """initial_purge の amount / resolved.source が期待値になるまで待つ."""

    def matches(config: dict[str, Any]) -> bool:
        initial = config["initial_purge"]
        resolved = initial["resolved"]
        return (amount is None or _approx(initial["initial_purge_ul"], amount)) and (
            resolved_source is None
            or (resolved is not None and resolved["source"] == resolved_source)
        )

    return wait_for_config(
        live_server,
        matches,
        f"initial_purge -> amount={amount!r}, source={resolved_source!r}",
    )


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
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        _open_paste_solder(browser_page, live_ui)

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

        visible_pad = browser_page.locator(f"{_testid('pad-polygon')}:visible").nth(0)
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

    def test_dataset_collection_layout_preview_draws_server_cells_without_control(
        self, live_ui: LiveUi, browser_page
    ):
        """レイアウト preview は操作権を持たない閲覧者にも描ける.

        装置を動かさない読み取り専用計算なので、閲覧だけで配置と撮影枚数が読める。

        ジョブフォームの入力自体は ``data-requires-control`` で inert になるため、
        設定変更の追従は別テスト（操作権あり）で見る。
        """
        browser_page.goto(
            f"{live_ui.base_url}/pasting/paste_volume_calibration",
            wait_until="domcontentloaded",
        )
        cells = browser_page.locator("#pdl-view .pdl-cell")
        cells.first.wait_for(state="attached", timeout=_BROWSER_TIMEOUT_MS)

        # 既定は 分割数 5 × サンプル数 3 + blank 4 = 19 点。
        assert cells.count() == 19
        summary = browser_page.locator("#pdl-summary").inner_text()
        assert "19" in summary
        assert browser_page.locator("#pdl-error").inner_text().strip() == ""

        # 吐出量は小数点 3 桁で表示する（既定 0.05〜0.2 uL の 5 分割）。
        legend = browser_page.locator("#pdl-legend").inner_text()
        assert "0.050 uL" in legend
        assert "0.088 uL" in legend  # 0.0875 の丸め
        assert "0.200 uL" in legend

    def test_dataset_collection_layout_preview_follows_the_form(
        self, live_ui: LiveUi, browser_page
    ):
        """設定変更でサーバが返した配置へ入れ替わる."""
        browser_page.goto(
            f"{live_ui.base_url}/pasting/paste_volume_calibration",
            wait_until="domcontentloaded",
        )
        _acquire_control(browser_page)
        cells = browser_page.locator("#pdl-view .pdl-cell")
        cells.first.wait_for(state="attached", timeout=_BROWSER_TIMEOUT_MS)

        browser_page.fill("#param-volume_divisions", "2")

        # 2 × 3 + blank 4 = 10 点へ入れ替わる。
        expect(cells).to_have_count(10, timeout=_BROWSER_TIMEOUT_MS)

    def test_dataset_collection_layout_preview_shows_the_reason_when_it_does_not_fit(
        self, live_ui: LiveUi, browser_page
    ):
        """収まらない設定でも preview を消さず理由を出す."""
        browser_page.goto(
            f"{live_ui.base_url}/pasting/paste_volume_calibration",
            wait_until="domcontentloaded",
        )
        _acquire_control(browser_page)
        browser_page.locator("#pdl-view .pdl-cell").first.wait_for(
            state="attached", timeout=_BROWSER_TIMEOUT_MS
        )

        browser_page.fill("#param-samples_per_volume", "500")

        error = browser_page.locator("#pdl-error")
        expect(error).not_to_have_text("", timeout=_BROWSER_TIMEOUT_MS)
        # 格子は残す（配置図ごと消さない）。
        assert browser_page.locator("#pdl-view .pdl-grid-cell").count() > 0

    def test_dataset_collection_renders_job_form_without_pad_editor(
        self, live_ui: LiveUi, browser_page
    ):
        """Dataset 収集は PCB 非依存の最小ジョブページ（pad editor を持たない）."""
        browser_page.goto(
            f"{live_ui.base_url}/pasting/paste_volume_calibration",
            wait_until="domcontentloaded",
        )
        _acquire_control(browser_page)
        browser_page.locator(_testid("job-form")).wait_for(
            state="visible", timeout=_BROWSER_TIMEOUT_MS
        )

        assert browser_page.locator(_testid("pad-viewer")).count() == 0
        assert browser_page.locator("#param-purge_pad_id").count() == 0
        for param_name in (
            "plate_width",
            "cell_size",
            "crop_size",
            "volume_min",
            "volume_divisions",
            "blank_count",
            "view_count",
            "shuffle_seed",
            "paste_id",
        ):
            browser_page.locator(f"#param-{param_name}").wait_for(
                state="visible", timeout=_BROWSER_TIMEOUT_MS
            )
        assert (
            browser_page.locator("#param-paste_lot").get_attribute(
                "data-param-optional"
            )
            == "true"
        )
        assert (
            browser_page.locator("#param-paste_id").get_attribute("data-param-optional")
            is None
        )
        for param_name in ("paste_id", "paste_lot"):
            param = browser_page.locator(f"#param-{param_name}").locator("..")
            widths = param.evaluate(
                """(el) => ({
                    param: el.getBoundingClientRect().width,
                    help: el.querySelector(".job-param-help")
                        .getBoundingClientRect().width,
                })"""
            )
            assert widths["help"] >= widths["param"] * 0.95

    def test_fake_camera_preview_image_loads(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        _open_paste_solder(browser_page, live_ui)

        preview = browser_page.locator(_testid("preview-img"))
        preview.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        browser_page.wait_for_function(
            """(img) => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0""",
            arg=preview.element_handle(),
            timeout=_BROWSER_TIMEOUT_MS,
        )

    def test_initial_purge_amount_persists_and_position_defaults_to_auto(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)

        _open_paste_solder(browser_page, live_ui)
        amount = browser_page.locator(_testid("pad-initial-purge-amount"))
        position = browser_page.locator(_testid("pad-initial-purge-pad"))
        clear_button = browser_page.locator(_testid("pad-clear-initial-purge-point"))
        amount.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        assert amount.input_value(timeout=_BROWSER_TIMEOUT_MS) == "0.1"
        # 自動は塗布順路先頭 pad の中心座標
        assert "自動" in position.text_content(timeout=_BROWSER_TIMEOUT_MS)
        assert clear_button.is_disabled()

        amount.fill("0.22")
        _wait_for_initial_purge(live_server, amount=0.22)

        browser_page.reload(wait_until="domcontentloaded")
        amount = browser_page.locator(_testid("pad-initial-purge-amount"))
        amount.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        assert amount.input_value(timeout=_BROWSER_TIMEOUT_MS) == "0.22"

        machine_toml = live_server.settings.config_dir / "machine.toml"
        assert "initial_purge_ul = 0.22" in machine_toml.read_text(encoding="utf-8")

    def test_copper_islands_are_drawn_under_the_pads(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        """銅箔はパージ位置を選ぶための背景として描き、当たり判定を持たない."""
        _select_led_blinker(live_server)

        _open_paste_solder(browser_page, live_ui)
        copper = browser_page.locator(_testid("pad-copper"))
        copper.first.wait_for(state="attached", timeout=_BROWSER_TIMEOUT_MS)

        assert copper.count() > 0
        assert (
            copper.first.evaluate("(el) => getComputedStyle(el).pointerEvents")
            == "none"
        )
        # パッドより先に描かれている（背面になる）
        assert browser_page.evaluate(
            """({copperSel, padSel}) => {
                const copperEl = document.querySelector(copperSel);
                const padEl = document.querySelector(padSel);
                return copperEl.compareDocumentPosition(padEl)
                    & Node.DOCUMENT_POSITION_FOLLOWING;
            }""",
            {"copperSel": _testid("pad-copper"), "padSel": _testid("pad-polygon")},
        )

    def test_purge_point_is_placed_by_clicking_the_board_and_persists(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)

        _open_paste_solder(browser_page, live_ui)
        set_point_button = browser_page.locator(_testid("pad-set-initial-purge-point"))
        pad_status = browser_page.locator(_testid("pad-initial-purge-pad"))
        marker = browser_page.locator(_testid("pad-purge-marker"))
        set_point_button.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        assert marker.count() == 0

        set_point_button.click()
        # マーカー打ち中はパッドが hover やカーソルで反応しない
        assert (
            browser_page.locator(f"{_testid('pad-polygon')}:visible").first.evaluate(
                "(el) => getComputedStyle(el).pointerEvents"
            )
            == "none"
        )
        # 基板ビューの中央をクリックした点がパージ位置になる（外形線は
        # fill:none でヒットしないので SVG 自体を叩く）
        browser_page.locator(_testid("pad-viewer")).click(force=True)
        purge = _wait_for_initial_purge(live_server, resolved_source="explicit")
        assert purge["initial_purge"]["point"] is not None

        browser_page.wait_for_function(
            """(selector) => document.querySelector(selector) !== null""",
            arg=_testid("pad-purge-marker"),
            timeout=_BROWSER_TIMEOUT_MS,
        )
        assert "mm" in pad_status.text_content(timeout=_BROWSER_TIMEOUT_MS)

        browser_page.reload(wait_until="domcontentloaded")
        browser_page.wait_for_function(
            """(selector) => document.querySelector(selector) !== null""",
            arg=_testid("pad-purge-marker"),
            timeout=_BROWSER_TIMEOUT_MS,
        )
        pad_status = browser_page.locator(_testid("pad-initial-purge-pad"))
        assert "mm" in pad_status.text_content(timeout=_BROWSER_TIMEOUT_MS)

        clear_button = browser_page.locator(_testid("pad-clear-initial-purge-point"))
        assert not clear_button.is_disabled()
        clear_button.click()
        _wait_for_initial_purge(live_server, resolved_source="default")
        browser_page.wait_for_function(
            """(selector) => document.querySelector(selector) === null""",
            arg=_testid("pad-purge-marker"),
            timeout=_BROWSER_TIMEOUT_MS,
        )


class TestPasteSolderBrowserPadInteraction:
    """実ブラウザ操作と API 永続化."""

    def test_pad_click_selects_single_pad_without_toggling_enabled(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        config = _get_pad_config(live_server)
        pad = next(pad for pad in config["pads"] if pad["layer"] == "Top")

        _open_paste_solder(browser_page, live_ui)
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
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        config = _get_pad_config(live_server)
        bottom_pad = next(pad for pad in config["pads"] if pad["layer"] == "Bottom")

        _open_paste_solder(browser_page, live_ui)
        browser_page.locator(_testid("pad-layer-bottom")).check()
        bottom_polygon = browser_page.locator(_pad_selector(bottom_pad["id"]))
        bottom_polygon.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)

        browser_page.locator(_testid("pad-disable-all")).click()
        _wait_for_layer_enabled(live_server, "Bottom", False)

        browser_page.locator(_testid("pad-enable-all")).click()
        _wait_for_layer_enabled(live_server, "Bottom", True)

    def test_enum_and_height_controls_persist_after_reload(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        _open_paste_solder(browser_page, live_ui)

        root_row = browser_page.locator(_row_selector("L0"))
        mode_select = _field_select(
            root_row, "dispense_mode", "pad-dispense-mode-select"
        )
        mode_select.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        assert "Auto" in mode_select.locator("option:checked").text_content(
            timeout=_BROWSER_TIMEOUT_MS
        )

        mode_select.select_option("line")
        _wait_for_override(live_server, "L0", "dispense_mode", "line")
        _open_paste_solder(browser_page, live_ui)
        root_row = browser_page.locator(_row_selector("L0"))
        mode_select = _field_select(
            root_row, "dispense_mode", "pad-dispense-mode-select"
        )
        assert mode_select.input_value(timeout=_BROWSER_TIMEOUT_MS) == "line"

        mode_select.select_option("area")
        _wait_for_override(live_server, "L0", "dispense_mode", "area")
        _open_paste_solder(browser_page, live_ui)
        root_row = browser_page.locator(_row_selector("L0"))
        mode_select = _field_select(
            root_row, "dispense_mode", "pad-dispense-mode-select"
        )
        assert mode_select.input_value(timeout=_BROWSER_TIMEOUT_MS) == "area"

        direction_select = _field_select(
            root_row, "line_direction", "pad-line-direction-select"
        )
        assert "指定なし" in direction_select.locator("option:checked").text_content(
            timeout=_BROWSER_TIMEOUT_MS
        )
        direction_select.select_option("outward")
        _wait_for_override(live_server, "L0", "line_direction", "outward")
        _open_paste_solder(browser_page, live_ui)
        root_row = browser_page.locator(_row_selector("L0"))
        direction_select = _field_select(
            root_row, "line_direction", "pad-line-direction-select"
        )
        assert direction_select.input_value(timeout=_BROWSER_TIMEOUT_MS) == "outward"

        height_select = _field_select(
            root_row, "paste_height", "pad-height-mode-select"
        )
        height_select.select_option("auto")
        _wait_for_override(live_server, "L0", "paste_height", "auto")
        # サーバー状態のポーリングだけでは patchNode 応答後の renderTable
        # 完了と順序保証がなく、旧 DOM へ操作した直後に再描画で input が
        # hidden な新要素へ差し替わるレースがあった。override マーカーの
        # 出現（再描画後にのみ存在する）で UI 反映完了を待つ。
        root_row.locator(
            '[data-testid="pad-own-override-marker"][data-field="paste_height"]'
        ).wait_for(state="attached", timeout=_BROWSER_TIMEOUT_MS)

        height_select.select_option("manual")
        height_input = _field_input(root_row, "paste_height")
        height_input.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        height_input.fill("0.25")
        height_input.press("Enter")
        _wait_for_override(live_server, "L0", "paste_height", 0.25)

    def test_tree_row_highlights_pads_by_node_id_membership(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        config = _get_pad_config(live_server)
        node_id = _choose_l3_node_id(config)
        expected_ids = {
            pad["id"] for pad in config["pads"] if node_id in pad["node_ids"]
        }
        assert expected_ids
        assert expected_ids != {pad["id"] for pad in config["pads"]}

        _open_paste_solder(browser_page, live_ui)
        path_ids = _tree_path_ids(config["tree"], node_id)
        assert path_ids
        row = _ensure_row_visible(browser_page, path_ids)

        # hover 退避は tree 行とも SVG ビューアとも重ならない既知要素へ移す
        # （座標 (1,1) だと viewport によりヘッダ等に載り不安定なため）
        row.hover()
        _wait_for_highlighted(browser_page, expected_ids)
        browser_page.locator(_testid("pad-editor-toolbar")).hover()
        _wait_for_highlighted(browser_page, set())

        row.click()
        browser_page.locator(_testid("pad-editor-toolbar")).hover()
        _wait_for_highlighted(browser_page, expected_ids)

    def test_route_button_draws_route_and_enabled_change_clears_it(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        route = _calculate_route(live_server)
        assert len(route["pads"]) > 1
        first = route["pads"][0]

        _open_paste_solder(browser_page, live_ui)
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
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        _patch_pad_config_node(live_server, "L0", {"dispense_mode": "area"})
        fill_path = _calculate_fill_path(live_server)
        assert fill_path["pads"]
        first = fill_path["pads"][0]

        _open_paste_solder(browser_page, live_ui)
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
        _wait_for_override(live_server, "L0", "boundary_margin", 0.3)
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

    def test_active_job_locks_pad_editor(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        _open_paste_solder(browser_page, live_ui)

        start = httpx.post(
            f"{live_server.base_url}/api/jobs/job_demo",
            json={"params": {"steps": 1, "interval": 0.1}},
            headers=_session_headers(browser_page),
            timeout=_HTTP_TIMEOUT,
        )
        assert start.status_code == 201, start.text

        browser_page.locator(".pad-editor-locked").wait_for(
            state="attached", timeout=_BROWSER_TIMEOUT_MS
        )
        assert browser_page.locator(_testid("pad-disable-all")).is_disabled()
        assert browser_page.locator(_testid("pad-setting-input")).nth(0).is_disabled()

    def test_saved_override_file_can_be_imported(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page, tmp_path: Path
    ):
        _select_led_blinker(live_server)
        patch = httpx.patch(
            f"{live_server.base_url}/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"prime_extra_delay": 0.33}},
            timeout=_HTTP_TIMEOUT,
        )
        assert patch.status_code == 200, patch.text
        exported = httpx.get(
            f"{live_server.base_url}/api/pasting/pad-config/export",
            timeout=_HTTP_TIMEOUT,
        )
        assert exported.status_code == 200, exported.text
        # export 後に override を公開 API で消し、import が復元することを見る
        cleared = httpx.patch(
            f"{live_server.base_url}/api/pasting/pad-config/node",
            json={"node": "L2:U1", "clear": ["prime_extra_delay"]},
            timeout=_HTTP_TIMEOUT,
        )
        assert cleared.status_code == 200, cleared.text
        assert "L2:U1" not in _get_pad_config(live_server)["overrides"]

        import_path = tmp_path / "paste-overrides.json"
        import_path.write_text(json.dumps(exported.json()), encoding="utf-8")
        _open_paste_solder(browser_page, live_ui)
        browser_page.locator("#pad-import-config").set_input_files(str(import_path))

        # ページ内 fetch は同一オリジン（frontend）へ投げる。backend 直だと
        # クロスオリジンになり、無認証 = CORS 無しの backend では必ず失敗する
        browser_page.wait_for_function(
            """async (baseUrl) => {
                const response = await fetch(`${baseUrl}/api/pasting/pad-config`);
                const config = await response.json();
                return config.overrides["L2:U1"]?.values?.prime_extra_delay === 0.33;
            }""",
            arg=live_ui.base_url,
            timeout=_BROWSER_TIMEOUT_MS,
        )


class TestPasteSolderBrowserOverrideVisibility:
    """階層 override の祖先表示と編集時の競合警告."""

    def test_descendant_override_stays_visible_and_specific_after_parent_edit(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        _patch_pad_config_node(live_server, "L2:U1", {"prime_extra_delay": 0.33})
        config = _get_pad_config(live_server)
        path = _tree_path_ids(config["tree"], "L2:U1")
        assert path == ["L0", "L1:SOT-23-6", "L2:U1"]

        _open_paste_solder(browser_page, live_ui)
        l1_row = browser_page.locator(_row_selector("L1:SOT-23-6"))
        l1_row.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        _ensure_row_collapsed(browser_page, "L1:SOT-23-6", "L2:U1")

        l1_row.locator(
            f".pad-col-node {_DESCENDANT_OVERRIDE_MARKER_SELECTOR}"
        ).first.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        l1_prime_extra_delay_cell = _field_cell(l1_row, "prime_extra_delay")
        l1_prime_extra_delay_cell.locator(
            _DESCENDANT_FIELD_MARKER_SELECTOR
        ).first.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)

        l1_prime_extra_delay = _field_input(l1_row, "prime_extra_delay")
        l1_prime_extra_delay.fill("0.77")
        l1_prime_extra_delay.press("Enter")

        browser_page.locator(".toast").filter(
            has_text=_DESCENDANT_WARNING_RE
        ).first.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        _wait_for_override(live_server, "L1:SOT-23-6", "prime_extra_delay", 0.77)
        wait_for_config(
            live_server,
            lambda config: all(
                _approx(pad["resolved"]["prime_extra_delay"], 0.33)
                for pad in config["pads"]
                if "L2:U1" in pad["node_ids"]
            ),
            "L2:U1 配下 pad の prime_extra_delay が 0.33 に解決",
        )

        l2_row = _ensure_row_visible(browser_page, ["L0", "L1:SOT-23-6", "L2:U1"])
        l2_prime_extra_delay_cell = _field_cell(l2_row, "prime_extra_delay")
        l2_prime_extra_delay_input = _field_input(l2_row, "prime_extra_delay")
        assert (
            l2_prime_extra_delay_input.input_value(timeout=_BROWSER_TIMEOUT_MS)
            == "0.33"
        )
        assert "override" in l2_prime_extra_delay_input.get_attribute("class")
        l2_prime_extra_delay_cell.locator(".pad-override-marker").wait_for(
            state="visible", timeout=_BROWSER_TIMEOUT_MS
        )
        l2_prime_extra_delay_cell.locator(".pad-cell-clear").wait_for(
            state="visible", timeout=_BROWSER_TIMEOUT_MS
        )

    def test_bulk_pad_enable_patch_updates_override_visibility_without_reload(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
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

        _open_paste_solder(browser_page, live_ui)
        l1_row = browser_page.locator(_row_selector("L1:SOT-23-6"))
        l1_row.wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
        _ensure_row_collapsed(browser_page, "L1:SOT-23-6", "L2:U1")

        browser_page.locator(_testid("pad-disable-all")).click()
        wait_for_config(
            live_server,
            lambda config: config["overrides"].get(target_l4, {}).get("enabled")
            is False,
            f"{target_l4} enabled override -> False",
        )

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
    """Desktop/mobile で主要パネルが横方向にはみ出さない."""

    def test_machine_control_collapses_to_handle_width(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        browser_page.set_viewport_size({"width": 1280, "height": 900})
        # localStorage を触る前にオリジンを確保する（実ページを開く。`/m/{id}` 単体は
        # 既定タブへの 307 なので、素の prefix を開くと余計な遷移が挟まる）
        browser_page.goto(f"{live_ui.base_url}/posctrl", wait_until="domcontentloaded")
        browser_page.evaluate("localStorage.removeItem('mc-sidebar-collapsed')")
        _open_paste_solder(browser_page, live_ui)

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
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)

        for name, width, height in _VIEWPORTS:
            browser_page.set_viewport_size({"width": width, "height": height})
            _open_paste_solder(browser_page, live_ui)
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
                    "pad-initial-purge-tools",
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
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)
        browser_page.set_viewport_size({"width": 390, "height": 844})
        _open_paste_solder(browser_page, live_ui)

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
