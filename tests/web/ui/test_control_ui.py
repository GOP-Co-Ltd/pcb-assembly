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

# `data-requires-control` を置いてよいテンプレートの許可リスト（契約 §5）。閲覧者が
# 押すと 423 になる操作（nozzle-cap/record・任意 G-code・Record/Quit の WS command・
# canny の設定保存）を持つテンプレートだけが載る。どの要素を塞ぐかまでは列挙しない
# （要素を足すたびに一覧も直すことになり、検出できるのは一覧の更新漏れだけになる）
_GATED_TEMPLATES = {
    "base.html",
    "settings.html",
    "dev/klipper_status.html",
    "dev/audio.html",
    "partials/job_form.html",
    "partials/machine_control.html",
    "partials/update_panel.html",
    "partials/job_console.html",
    "partials/loading_controls.html",
    "partials/calibration_menu.html",
    "partials/pad_editor.html",
    "pasting/dispense_calibration.html",
    "pasting/paste_volume_calibration.html",
    "pasting/loading.html",
    "pasting/nozzle_cap.html",
    "pasting/paste_solder.html",
    "posctrl/copper_detection.html",
    "posctrl/reference_point_setup.html",
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

    def test_rendered_page_starts_as_viewer(self, frontend_client: TestClient):
        """SSR は backend へのサーバ間通信なので「自分が保持者か」を判定できない."""
        response = frontend_client.get(f"/m/{MACHINE_ID}/posctrl")

        assert response.status_code == 200, response.text
        assert 'data-control="viewer"' in response.text


class TestInertIsTheOnlyMechanism:
    """無効化は `inert` 1 種類（`.disabled` との二重管理を避ける）."""

    def test_control_js_is_the_only_writer_of_inert(self):
        """無効化を書くのは control.js だけ（JS テストランナーが無いので静的に見張る）.

        ジョブ状態で `.disabled` を書く 4 モジュールと同じ属性を使うと「ジョブ終了時に
        閲覧者のボタンが復活する」二重管理バグになる。
        初期状態の適用も control.js に任せ、テンプレートは印だけ持つ。
        """
        control_js = _js("control.js")

        wrote_disabled = [
            line
            for line in control_js.splitlines()
            if "disabled" in line and not line.lstrip().startswith("//")
        ]
        js_writers = {path.name for path in _js_files() if "inert" in _read(path)}
        templates_with_inert = {
            path.name
            for path in _template_files()
            if re.search(r"\binert\b", _read(path))
        }

        assert wrote_disabled == []
        assert js_writers == {"control.js"}
        assert templates_with_inert == set()


class TestGatedElements:
    """`data-requires-control` を付ける対象（契約 §5）."""

    def test_no_other_template_gates_anything(self):
        """一覧に無いテンプレートが勝手にゲートしていない（漏れの検出）."""
        unexpected = {
            str(path.relative_to(_TEMPLATES_DIR))
            for path in _template_files()
            if _gated_elements(_read(path))
            and str(path.relative_to(_TEMPLATES_DIR)) not in _GATED_TEMPLATES
        }

        assert unexpected == set()

    def test_control_lease_bar_is_never_gated(self):
        """操作権を取る唯一の入口なので閲覧者が触れる."""
        assert _gated_elements(_template("partials/control_lease.html")) == set()

    @pytest.mark.parametrize("element_id", ["estop", "jc-abort"])
    def test_safety_controls_are_ungated_in_the_rendered_page(
        self, frontend_client: TestClient, element_id: str
    ):
        """緊急停止と中止は閲覧者からも効かなければならない（詰みの回避と安全確認）."""
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


class TestScriptWiring:
    """読み込み配線（control.js は全ページ・settings にも WS）."""

    def test_control_js_is_loaded_before_the_scripts_block(self):
        """job_console.js より先に `window.webui.control` が生えている必要がある.

        job_console.js は子テンプレートの `{% block scripts %}` で読むため、順序が逆だと
        `control_changed` の受け口が未定義になる。
        """
        base_html = _template("base.html")

        assert base_html.index("js/control.js") < base_html.index("{% block scripts %}")

    @pytest.mark.parametrize("suffix", ["posctrl", "settings"])
    def test_rendered_pages_load_control_and_websocket(
        self, frontend_client: TestClient, suffix: str
    ):
        """`/settings` に WS が無いとリース状態（control_changed）が届かない."""
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
