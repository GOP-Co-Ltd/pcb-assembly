"""2 端末（別 browser context = 別セッション）から同じマシンを触ったときの操作権 E2E.

MR6 実装契約 §5 / §6 / §10 の交差検証。backend 単体テストではヘッダを注入しない
クライアントが全員 `anonymous` を共有してしまうため「別セッション」が作れず、
frontend 単体テストでは `inert` を実 DOM で確かめられない。ここだけが両方を見る。

アサートは 3 点に絞る:

- `body[data-control]`（``"held" | "viewer" | "free" | "unknown"``）
- 対象要素の `inert` 属性（**Playwright の `is_enabled()` は inert を見ない**ので
  属性そのものを見る）
- 押してもサーバ側に効果が無いこと（HTTP が飛ばない / backend が 423 で拒む）

安全系（緊急停止・中止）は閲覧者からでも効くことを、**サーバ側の観測可能な効果**で
確かめる。`_klipper_action` は Klipper 送信より先に abort フラグを立てるので、
Klipper 不通（テスト config は port 7126 = 非リッスン）でも実行中ジョブが中止される。

セッションの分離は cookie で行う（frontend は自称の
`X-Pcbasm-Session` を落として cookie から組み直すため、ヘッダでは分けられない）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from playwright.sync_api import expect
from starlette.types import ASGIApp, Receive, Scope, Send

from tests.e2e.conftest import (
    E2E_MACHINE_ID,
    LiveServer,
    LiveUi,
    acquire_control as _acquire_control,
    make_ui_settings,
    select_led_blinker as _select_led_blinker,
    session_headers as _session_headers,
    start_app,
)
from tests.helpers import wait_until
from web.ui.app import create_app
from web.ui.machines import MachineEndpoint

_HTTP_TIMEOUT = 10.0
_BROWSER_TIMEOUT_MS = 10_000

# 確認プロンプトで止まるジョブとそのページ（中止するまで終わらないので、閲覧者の
# 安全操作がサーバへ届いたことをジョブのステータスで観測できる）
_PROMPT_JOB = "height_plane"
_PROMPT_PAGE = "pasting/height_plane"


class _HttpOnlyTransport:
    """実 frontend の WS 接続を拒否し、状態の HTTP 応答を明示的に再開できる。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._state_received = Event()
        self._state_release = Event()

    def wait_state_request(self) -> bool:
        return self._state_received.wait(timeout=10)

    def resume_state(self) -> None:
        self._state_release.set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # accept 前に拒否するので、ブラウザの open は発火しない。
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] == "http" and scope["path"].endswith("/api/state"):
            self._state_received.set()
            await asyncio.to_thread(self._state_release.wait, 10)
        await self._app(scope, receive, send)


@pytest.fixture
def http_only_ui(
    live_server: LiveServer, tmp_path: Path
) -> Iterator[tuple[LiveUi, _HttpOnlyTransport]]:
    endpoint = MachineEndpoint(
        machine_id=E2E_MACHINE_ID, host="127.0.0.1", port=live_server.port
    )
    transport = _HttpOnlyTransport(
        create_app(
            make_ui_settings((endpoint,), machines_file=tmp_path / "absent.toml")
        )
    )
    server = start_app(transport)
    try:
        yield (
            LiveUi(
                origin=f"http://127.0.0.1:{server.port}", machine_ids=(E2E_MACHINE_ID,)
            ),
            transport,
        )
    finally:
        transport.resume_state()
        server.stop()


def _open(page: Any, live_ui: LiveUi) -> None:
    """ジョブコンソールのあるページを開き、操作権の状態がサーバ由来になるまで待つ."""
    page.goto(f"{live_ui.base_url}/{_PROMPT_PAGE}", wait_until="domcontentloaded")
    page.locator("#job-console").wait_for(state="visible", timeout=_BROWSER_TIMEOUT_MS)
    page.wait_for_function(
        "() => window.webui?.control !== undefined", timeout=_BROWSER_TIMEOUT_MS
    )
    page.evaluate("() => window.webui.control.refresh()")


def _open_http_only(page: Any, live_ui: LiveUi) -> None:
    """HTTP だけが使える frontend で control.js の初期化を待つ（状態の応答は待たない）."""
    page.goto(f"{live_ui.base_url}/{_PROMPT_PAGE}", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.webui?.control !== undefined", timeout=_BROWSER_TIMEOUT_MS
    )


def _release_control(live_server: LiveServer) -> None:
    """下準備の backend 直叩き（セッションヘッダ無し = anonymous）が握った操作権を返す."""
    response = httpx.post(
        f"{live_server.base_url}/api/control/release", timeout=_HTTP_TIMEOUT
    )
    assert response.status_code == 200, response.text
    assert response.json()["control"]["held"] is False


def _wait_control(page: Any, expected: str) -> None:
    page.wait_for_function(
        "(expected) => document.body.dataset.control === expected",
        arg=expected,
        timeout=_BROWSER_TIMEOUT_MS,
    )


def _is_inert(page: Any, selector: str) -> bool:
    """`inert` 属性が付いているか（`is_enabled()` は inert を見ないので属性を読む）."""
    return page.locator(selector).evaluate("(el) => el.hasAttribute('inert')")


def _job_status(live_server: LiveServer) -> str | None:
    job = httpx.get(
        f"{live_server.base_url}/api/jobs/current", timeout=_HTTP_TIMEOUT
    ).json()["job"]
    return None if job is None else job["status"]


def _operator_and_viewer(live_ui: LiveUi, browser_pages: Any) -> tuple[Any, Any]:
    """保持者のページと閲覧者のページを用意する（別 context = 別 cookie jar）."""
    operator = browser_pages()
    viewer = browser_pages()
    for page in (operator, viewer):
        _open(page, live_ui)

    _acquire_control(operator)
    _wait_control(operator, "held")
    # 保持者の変更は WS の control_changed で全 subscriber へ届く（再読込は不要）
    _wait_control(viewer, "viewer")
    return operator, viewer


def _start_prompt_job(live_server: LiveServer, operator: Any) -> None:
    """保持者のセッションでプロンプト待ちのジョブを開始する."""
    response = httpx.post(
        f"{live_server.base_url}/api/jobs/{_PROMPT_JOB}",
        headers=_session_headers(operator),
        timeout=_HTTP_TIMEOUT,
    )
    assert response.status_code == 201, response.text
    wait_until(
        lambda: _job_status(live_server) == "waiting_input",
        timeout=30.0,
        interval=0.05,
    )


class TestTwoBrowsersOnOneMachine:
    """操作権を持つ端末と持たない端末の差（実 DOM の inert とサーバ側効果）."""

    def test_holder_and_viewer_diverge_without_a_reload(
        self, live_server: LiveServer, live_ui: LiveUi, browser_pages
    ):
        _select_led_blinker(live_server)
        operator, viewer = _operator_and_viewer(live_ui, browser_pages)

        assert not _is_inert(operator, "#firmware-restart")
        assert not _is_inert(operator, "#machine-control")

        assert _is_inert(viewer, "#firmware-restart")
        assert _is_inert(viewer, "#machine-control")
        assert _is_inert(viewer, ".jc-prompt-actions")
        assert _is_inert(viewer, "#jc-prompt-field")

    def test_gated_controls_are_inert_before_the_server_answers(
        self, live_server: LiveServer, http_only_ui, browser_pages
    ):
        """状態が届く前は塞ぐ（fail-closed）.

        SSR の `data-control="viewer"` だけでは `inert` は付かない（付けるのは
        control.js の初期描画）。初期値が `held` だと応答が届くまでの間は操作できて
        しまい、既に他の端末が持っている操作へ割り込める。`GET /api/state` を
        実 frontend の手前で保留し、その窓を固定して観測する。再開後は実 backend の
        空き状態が届くことも確認し、未完了リクエストを残さない。
        """
        _select_led_blinker(live_server)
        _release_control(live_server)
        ui, transport = http_only_ui
        page = browser_pages()
        _open_http_only(page, ui)
        assert transport.wait_state_request()

        assert page.evaluate("() => document.body.dataset.control") == "viewer"
        assert _is_inert(page, "#firmware-restart")
        assert _is_inert(page, "#machine-control")
        assert not _is_inert(page, "#estop")

        transport.resume_state()
        _wait_control(page, "free")

    def test_viewer_click_on_a_gated_control_reaches_nothing(
        self, live_server: LiveServer, live_ui: LiveUi, browser_pages
    ):
        _select_led_blinker(live_server)
        _, viewer = _operator_and_viewer(live_ui, browser_pages)

        urls: list[str] = []
        viewer.on("request", lambda request: urls.append(request.url))

        # 実マウスで押す（inert はポインタイベントを通さないので何も起きない）
        button = viewer.locator("#firmware-restart")
        box = button.bounding_box(timeout=_BROWSER_TIMEOUT_MS)
        assert box is not None
        viewer.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        # 「必ず飛ぶ」リクエストを 1 本入れて順序で「飛んでいない」を確かめる
        # （ハンドラが動いていれば fetch はこれより前に並ぶ）
        viewer.evaluate("() => window.webui.control.refresh()")
        assert [url for url in urls if "/api/firmware-restart" in url] == []

        # UI を迂回されても backend が拒む（fail-closed の二重化）
        denied = httpx.post(
            f"{live_server.base_url}/api/firmware-restart",
            headers=_session_headers(viewer),
            timeout=_HTTP_TIMEOUT,
        )
        assert denied.status_code == 423, denied.text
        assert denied.json()["holder"]["held"] is True

    @pytest.mark.parametrize("width", [1280, 390])
    def test_viewer_can_stop_a_running_job(
        self, live_server: LiveServer, live_ui: LiveUi, browser_pages, width: int
    ):
        """安全系（緊急停止・中止）は閲覧者のままでも塞がれない.

        WS 越しの abort 中継そのものは
        tests/e2e/test_proxy_e2e.py::TestJobOverProxiedWebSocket が担当する。
        """
        _select_led_blinker(live_server)
        operator, viewer = _operator_and_viewer(live_ui, browser_pages)
        _start_prompt_job(live_server, operator)
        viewer.set_viewport_size({"width": width, "height": 844})
        viewer.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # 中止も緊急停止も閲覧者の画面で押せる（inert が付かない）
        assert not _is_inert(viewer, "#jc-abort")
        expect(viewer.locator("#jc-abort")).to_be_enabled(timeout=_BROWSER_TIMEOUT_MS)
        assert not _is_inert(viewer, "#estop")
        # 自動スクロールで救済される前に、画面内で指が届き、他の要素に覆われないこと。
        stop = viewer.get_by_test_id("estop")
        expect(stop).to_have_count(1)
        assert stop.evaluate("""el => {
            const box = el.getBoundingClientRect();
            return box.top >= 0 && box.bottom <= innerHeight &&
                document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2) === el;
        }""")

        with viewer.expect_response(
            lambda response: response.url.endswith("/api/emergency-stop")
        ) as info:
            viewer.locator("#estop").click(timeout=_BROWSER_TIMEOUT_MS)
        # Klipper 不通なので 502 になるが、操作権では塞がれない（423 にならない）
        assert info.value.status != 423
        wait_until(
            lambda: _job_status(live_server) == "aborted", timeout=30.0, interval=0.05
        )

    def test_load_fetches_the_lease_state_without_the_websocket(
        self, live_server: LiveServer, http_only_ui, browser_pages
    ):
        """更新源 (a): ロード時の `GET /api/state` だけで状態がサーバ由来になる.

        SSR の初期値は `viewer` なので、空きリースが `free` として届いたことが
        「ロード後にサーバへ問い合わせた」証跡になる（この 1 往復が無いと、空いて
        いるのに `取得` が出ないページになる）。WS の open でも取り直すため、
        WS を開かないブラウザで control.js の分だけを切り出す。
        """
        _select_led_blinker(live_server)
        # 直叩きの下準備が anonymous として握っているので返させる（空きから始める）
        _release_control(live_server)
        page = browser_pages()

        ui, transport = http_only_ui
        transport.resume_state()
        _open_http_only(page, ui)

        _wait_control(page, "free")

    def test_denied_write_drops_the_page_to_viewer(
        self, live_server: LiveServer, http_only_ui, browser_pages
    ):
        """更新源 (c): `api()` の 423 で `onDenied` が保持者表示を畳む.

        WS が切れている間に奪われると、ページは自分が保持者だと思ったまま操作を送る。
        423 を fail-open に扱う（`held` のまま）と、操作できる見た目が残って押すたびに
        エラートーストが出る画面になる。既存の 423 検証は httpx の直叩きで
        ブラウザの `api()` 経路を通らないため、ここだけが `onDenied` を踏む。
        """
        _select_led_blinker(live_server)
        page = browser_pages()
        ui, transport = http_only_ui
        transport.resume_state()
        _open_http_only(page, ui)
        _acquire_control(page)
        # WS が黙っているので control_changed が届かない = ページは held のまま残る
        assert (
            httpx.post(
                f"{live_server.base_url}/api/control/takeover",
                headers={"X-Pcbasm-Session": "another-terminal"},
                timeout=_HTTP_TIMEOUT,
            ).status_code
            == 200
        )
        assert not _is_inert(page, "#firmware-restart")

        status = page.evaluate(
            """async () => {
                try {
                    await window.webui.api("POST", "/api/firmware-restart");
                    return 200;
                } catch (err) {
                    return err.status;
                }
            }"""
        )

        assert status == 423
        _wait_control(page, "viewer")
        assert _is_inert(page, "#firmware-restart")

    def test_viewer_can_take_over_the_lease(
        self, live_server: LiveServer, live_ui: LiveUi, browser_pages
    ):
        _select_led_blinker(live_server)
        operator, viewer = _operator_and_viewer(live_ui, browser_pages)

        viewer.locator("#control-takeover").click(timeout=_BROWSER_TIMEOUT_MS)

        _wait_control(viewer, "held")
        # 元の保持者は再読込なしで閲覧者へ落ちる（control_changed の同一 payload を
        # 各クライアントが you.key と比べて判定する）
        _wait_control(operator, "viewer")
        assert not _is_inert(viewer, "#firmware-restart")
        assert _is_inert(operator, "#firmware-restart")
