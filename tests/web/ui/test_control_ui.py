"""操作権リースの frontend UI（`control.js` とテンプレートの印）の仕様テスト.

契約書 /tmp/pcbasm-plan/mr6-brief.md §5 と §10「DOM 契約」が契約:

- `body.dataset.control`（``"held" | "viewer" | "free" | "unknown"``）の 1 箇所で
  全体を切り替える。**初期値は viewer（fail-closed）**
- 無効化手段は **`inert` 属性 1 種類**に統一する（`.disabled` はジョブ状態を見て
  4 モジュールが既に書いており、同じ属性を使うと「ジョブ終了時に閲覧者のボタンが
  復活する」二重管理バグになる）
- 更新源は (a) ページロード後の `GET /api/state`、(b) WS の `control_changed`、
  (c) `api()` の 423 の 3 つだけ
- **`#estop` と `#jc-abort` は絶対にゲートしない**（安全操作）
- prompt 本文は閲覧者にも見せ、案内（誰の応答待ちか / 操作権が空いている）を出す
- `settings.html` も `job_console.js` を読む（WS が無いとリース状態が届かない）

JS にテストランナーが無いので、切替ロジックそのものは**ソースの契約点をピンする**形で
押さえる（`tests/web/ui/test_layout.py::TestMachinePrefixFunnel` と同じ手段）。
実 DOM 上の `inert` と 2 ブラウザの挙動は e2e（Playwright）が持つ。
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web.ui import layout

_UI_DIR = Path(layout.__file__).parent
_TEMPLATES_DIR = _UI_DIR / "templates"
_JS_DIR = _UI_DIR / "static" / "js"

MACHINE_ID = "uitest"

# data-requires-control を持つ要素の抽出（id が無い要素は先頭 class で表す）
_GATED_TAG_RE = re.compile(r"<[a-zA-Z][^>]*data-requires-control[^>]*>", re.DOTALL)
_ID_RE = re.compile(r'\bid="([^"]+)"')
_CLASS_RE = re.compile(r'\bclass="([^"]+)"')

# 契約 §5 のリストをテンプレート単位に落としたもの。閲覧者が押すと 423 になる操作
# （nozzle-cap/record・任意 G-code・Record/Quit の WS command・canny の設定保存）も
# 同じ規則で塞ぐ
_GATED_ELEMENTS = {
    "base.html": {"#pcb-chip", "#firmware-restart"},
    "settings.html": {"#machine-settings-form"},
    "dev/klipper_status.html": {"#ks-gcode-form"},
    # 通知音のテスト再生は機体のスピーカーが実際に鳴る（POST /api/audio/test と対応）
    "dev/audio.html": {"#audio-settings-form", ".audio-test-actions"},
    "partials/job_form.html": {"#job-form"},
    "partials/machine_control.html": {"#machine-control"},
    "partials/loading_controls.html": {"#loading-controls"},
    "partials/calibration_menu.html": {"#calibration-menu"},
    "partials/job_console.html": {
        "#jc-apply",
        "#jc-prompt-field",
        ".jc-prompt-actions",
    },
    "partials/pad_editor.html": {
        ".pad-select-tools",
        ".pad-initial-purge-tools",
        "#pad-import-config-button",
        "#pad-import-config",
        "#pad-table-body",
    },
    "pasting/dispense_calibration.html": {"#job-form"},
    "pasting/paste_dataset_collection.html": {"#job-form"},
    "pasting/loading.html": {
        "#job-form",
        "#lc-apply-rotations-per-ul",
        "#lc-apply-dispense-rate",
        "#lc-apply-dispense-accel",
        "#lc-apply-all",
    },
    "pasting/nozzle_cap.html": {"#nc-record"},
    "pasting/paste_solder.html": {".paste-auto-thresholds"},
    "posctrl/copper_detection.html": {"#canny-save"},
    "posctrl/reference_point_setup.html": {".rps-actions"},
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _template(name: str) -> str:
    return _read(_TEMPLATES_DIR / name)


def _js(name: str) -> str:
    return _read(_JS_DIR / name)


def _template_files() -> list[Path]:
    return sorted(_TEMPLATES_DIR.rglob("*.html"))


def _js_files() -> list[Path]:
    return sorted(_JS_DIR.rglob("*.js"))


def _gated_elements(html: str) -> set[str]:
    markers = set()
    for tag in _GATED_TAG_RE.findall(html):
        if (found := _ID_RE.search(tag)) is not None:
            markers.add(f"#{found.group(1)}")
        elif (found := _CLASS_RE.search(tag)) is not None:
            markers.add(f".{found.group(1).split()[0]}")
        else:
            markers.add(tag)
    return markers


def _element_tag(html: str, element_id: str) -> str:
    """Id で 1 要素の開始タグを取り出す（属性の有無を見るため）."""
    found = re.search(rf'<[a-zA-Z][^>]*\bid="{element_id}"[^>]*>', html, re.DOTALL)
    assert found is not None, element_id
    return found.group(0)


class TestFailClosedInitialState:
    """初期値は viewer（サーバの事実が届く前に押せてはいけない）."""

    def test_body_starts_as_viewer(self):
        """SSR は backend へのサーバ間通信なので「自分が保持者か」を判定できない."""
        assert 'data-control="viewer"' in _template("base.html")

    def test_control_js_starts_as_viewer(self):
        assert re.search(
            r'const INITIAL_STATE = "viewer";', _js("control.js")
        ), "control.js の初期状態は viewer（fail-closed）"

    def test_only_the_holder_is_ungated(self):
        """Held 以外（unknown を含む）はすべて塞ぐ."""
        assert 'const blocked = state !== "held";' in _js("control.js")

    def test_rendered_page_starts_as_viewer(self, frontend_client: TestClient):
        response = frontend_client.get(f"/m/{MACHINE_ID}/posctrl")

        assert response.status_code == 200, response.text
        assert 'data-control="viewer"' in response.text


class TestInertIsTheOnlyMechanism:
    """無効化は `inert` 1 種類（`.disabled` との二重管理を避ける）."""

    def test_control_js_toggles_inert(self):
        assert 'toggleAttribute("inert"' in _js("control.js")

    def test_control_js_never_writes_disabled(self):
        """ジョブ状態で `.disabled` を書く 4 モジュールと同じ属性を使わない."""
        code = [
            line
            for line in _js("control.js").splitlines()
            if "disabled" in line and not line.lstrip().startswith("//")
        ]

        assert code == []

    def test_control_js_is_the_only_writer_of_inert(self):
        writers = {path.name for path in _js_files() if "inert" in _read(path)}

        assert writers == {"control.js"}

    def test_templates_do_not_hardcode_inert(self):
        """初期状態の適用も control.js に任せる（テンプレートは印だけ持つ）."""
        hardcoded = {
            path.name
            for path in _template_files()
            if re.search(r"\binert\b", _read(path))
        }

        assert hardcoded == set()


class TestGatedElements:
    """`data-requires-control` を付ける対象（契約 §5）."""

    @pytest.mark.parametrize(("name", "expected"), sorted(_GATED_ELEMENTS.items()))
    def test_expected_elements_are_gated(self, name: str, expected: set[str]):
        assert _gated_elements(_template(name)) == expected

    def test_no_other_template_gates_anything(self):
        """一覧に無いテンプレートが勝手にゲートしていない（漏れの検出）."""
        unexpected = {
            str(path.relative_to(_TEMPLATES_DIR))
            for path in _template_files()
            if _gated_elements(_read(path))
            and str(path.relative_to(_TEMPLATES_DIR)) not in _GATED_ELEMENTS
        }

        assert unexpected == set()

    def test_emergency_stop_is_never_gated(self):
        """緊急停止は閲覧者からも効かなければならない（安全確認）."""
        assert "data-requires-control" not in _element_tag(
            _template("base.html"), "estop"
        )

    def test_job_abort_is_never_gated(self):
        """中止は閲覧者からも効かなければならない（詰みの回避と安全確認）."""
        assert "data-requires-control" not in _element_tag(
            _template("partials/job_console.html"), "jc-abort"
        )

    def test_control_lease_bar_is_never_gated(self):
        """操作権を取る唯一の入口なので閲覧者が触れる."""
        assert _gated_elements(_template("partials/control_lease.html")) == set()

    @pytest.mark.parametrize("element_id", ["estop", "jc-abort"])
    def test_safety_controls_are_ungated_in_the_rendered_page(
        self, frontend_client: TestClient, element_id: str
    ):
        response = frontend_client.get(f"/m/{MACHINE_ID}/pasting/paste_solder")

        assert response.status_code == 200, response.text
        assert "data-requires-control" not in _element_tag(response.text, element_id)

    def test_gated_controls_are_marked_in_the_rendered_page(
        self, frontend_client: TestClient
    ):
        response = frontend_client.get(f"/m/{MACHINE_ID}/pasting/paste_solder")

        assert response.status_code == 200, response.text
        for element_id in ("pcb-chip", "firmware-restart", "machine-control"):
            assert "data-requires-control" in _element_tag(response.text, element_id)


class TestUpdateSources:
    """状態の更新源は 3 つだけ（契約 §5）."""

    def test_control_js_reads_the_state_endpoint(self):
        """更新源 (a) の宛先のピン.

        「ロード時に実際に取りに行く」ことはソースからは分からない（`refresh()` の
        呼び出しを消しても文字列は残る）。実挙動は
        `tests/e2e/test_multi_user_browser.py` の
        `test_load_fetches_the_lease_state_without_the_websocket` が持つ。
        """
        assert 'api("GET", "/api/state")' in _js("control.js")

    def test_websocket_control_changed_is_relayed(self):
        assert 'case "control_changed":' in _js("job_console.js")
        assert "window.webui.control?.applyControl(event.control);" in _js(
            "job_console.js"
        )

    def test_reconnect_resyncs_the_lease(self):
        """切断中の control_changed は届かないので再接続で取り直す."""
        assert "window.webui.control?.refresh();" in _js("job_console.js")

    def test_locked_response_notifies_control_js(self):
        assert "if (res.status === 423) window.webui.control?.onDenied(data);" in _js(
            "app.js"
        )

    def test_api_errors_carry_status_and_body(self):
        """既存 20 箇所は `err.message` しか読まないので後方互換."""
        app_js = _js("app.js")

        assert "err.status = res.status;" in app_js
        assert "err.data = data;" in app_js

    def test_control_js_exposes_the_denied_hook(self):
        control_js = _js("control.js")

        assert "window.webui.control = {" in control_js
        assert "onDenied," in control_js


class TestPromptVisibility:
    """プロンプトは閲覧者にも見せ、応答権の在処を案内する（契約 §4 / §5）."""

    def test_prompt_message_is_not_gated(self):
        """本文を隠すと、何を待っているのか閲覧者に分からない."""
        job_console = _template("partials/job_console.html")

        assert "data-requires-control" not in _element_tag(
            job_console, "jc-prompt-message"
        )

    def test_prompt_hint_element_exists(self):
        assert 'id="jc-prompt-hint"' in _template("partials/job_console.html")

    def test_viewer_is_told_who_is_being_waited_for(self):
        assert "の応答待ち" in _js("control.js")

    def test_free_lease_asks_to_acquire_it(self):
        """`pending_prompt` があるのに holder が null のときの案内."""
        assert "操作権が空いています。取得して応答してください" in _js("control.js")


class TestScriptWiring:
    """読み込み配線（control.js は全ページ・settings にも WS）."""

    def test_control_js_is_loaded_on_every_page(self):
        base_html = _template("base.html")

        assert "js/control.js" in base_html
        # job_console.js は子テンプレートの {% block scripts %} で読むので、
        # window.webui.control が先に生えていなければならない
        assert base_html.index("js/control.js") < base_html.index("{% block scripts %}")

    def test_settings_page_loads_the_websocket_client(self):
        """`/settings` に WS が無いとリース状態（control_changed）が届かない."""
        assert "js/job_console.js" in _template("settings.html")

    @pytest.mark.parametrize("suffix", ["posctrl", "settings"])
    def test_rendered_pages_load_control_and_websocket(
        self, frontend_client: TestClient, suffix: str
    ):
        response = frontend_client.get(f"/m/{MACHINE_ID}/{suffix}")

        assert response.status_code == 200, response.text
        assert "js/control.js" in response.text
        assert "js/job_console.js" in response.text

    def test_control_lease_bar_is_rendered_on_every_page(
        self, frontend_client: TestClient
    ):
        response = frontend_client.get(f"/m/{MACHINE_ID}/settings")

        assert response.status_code == 200, response.text
        for element_id in (
            "control-holder",
            "control-name",
            "control-acquire",
            "control-release",
            "control-takeover",
        ):
            assert f'id="{element_id}"' in response.text

    def test_machine_independent_page_has_no_control_bar(
        self, frontend_client: TestClient
    ):
        """マシンが決まっていない案内ページに操作権の概念は無い.

        `message.html`（ピッカー / 503 / 未登録 404）は `base` が空なので、
        `control.js` の `GET /api/state` と取得 POST がプロキシ外へ飛んで 404 になる。
        バーを描くと「押すと必ず失敗する取得ボタン」が出る。
        """
        response = frontend_client.get("/m/no-such-machine/posctrl")

        assert response.status_code == 404, response.text
        assert 'id="control-lease"' not in response.text
        assert 'id="control-acquire"' not in response.text
