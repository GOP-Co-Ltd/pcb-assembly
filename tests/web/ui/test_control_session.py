"""操作権リースのセッション同定（frontend 側）の仕様テスト.

契約書 /tmp/pcbasm-plan/mr6-brief.md §2 と §10 が契約:

- frontend は **HTML ページ応答でのみ** `pcbasm_session`（httpOnly,
  `secrets.token_urlsafe(16)`）を発行し、**`Path=/` の既定を上書きしない**
  （`/m/{id}/api/**` と WS ハンドシェイクと `img.src` に cookie が乗ることが、
  「ブラウザは独自ヘッダを付けられない」制約の唯一の抜け道）
- `ProxyApp` の**ヘッダ組み立て 1 箇所**で cookie を
  `X-Pcbasm-Session` / `X-Pcbasm-Client-Name`（`quote` 済み）へ翻訳して backend に渡す
- cookie 自体は従来どおり backend へ渡さない

**上流はエコー ASGI アプリ**（実 backend ではない）。ワイヤ契約（§10）だけを前提に
frontend を検証するため、backend 側の実装状況に依存しない。backend が受け取った
ヘッダは実 backend では観測できない（識別子を応答に出さない契約）ので、エコー上流から
確かめる必要もある。

WS ハンドシェイクのヘッダ注入は同じ `_forward_headers` を通るが、`ASGITransport` は
websocket scope を扱えないため in-process では検証できない（実 uvicorn の e2e が持つ）。
"""

import json
from collections.abc import Iterator
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from web.ui.proxy import NAME_COOKIE, SESSION_COOKIE
from web.ui.settings import Settings

# tests/web/ui/conftest.py の静的登録 1 台と同じ id
MACHINE_ID = "uitest"

# ブラウザが JS から書く表示名 cookie（encodeURIComponent 済み）
QUOTED_NAME = "%E7%94%B0%E4%B8%AD"


async def _echo_backend(scope: Scope, receive: Receive, send: Send) -> None:
    """受け取ったリクエストヘッダを JSON で返す上流 ASGI アプリ."""
    while True:
        message = await receive()
        if not message.get("more_body", False):
            break
    payload = json.dumps(
        {
            "path": scope["path"],
            "headers": {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope["headers"]
            },
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": payload})


@pytest.fixture
def echo_frontend(ui_settings: Settings) -> Iterator[TestClient]:
    """エコー上流を挿した frontend（ページ + プロキシの実配線）.

    SSR ページはエコーの JSON が契約モデルを満たさないため 503 の案内ページになる。 案内ページも HTML
    ページなのでセッション cookie は発行される（発行点が 1 箇所で あることの確認材料にもなる）。
    """
    from web.ui.app import create_app

    app = create_app(
        ui_settings,
        transport_factory=lambda _endpoint: httpx.ASGITransport(app=_echo_backend),
    )
    with TestClient(app) as client:
        yield client


def _session_cookie_headers(response: httpx.Response) -> list[str]:
    return [
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{SESSION_COOKIE}=")
    ]


def _echoed_headers(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 200, response.text
    return response.json()["headers"]


class TestSessionCookie:
    """HTML ページ応答でのみ発行するセッション cookie."""

    def test_html_page_issues_an_httponly_session_cookie(
        self, frontend_client: TestClient
    ):
        response = frontend_client.get(f"/m/{MACHINE_ID}/posctrl")

        assert response.status_code == 200, response.text
        (issued,) = _session_cookie_headers(response)
        assert "HttpOnly" in issued
        assert frontend_client.cookies[SESSION_COOKIE] != ""

    def test_session_cookie_is_scoped_to_the_site_root(
        self, frontend_client: TestClient
    ):
        """`Path` を狭めると MJPEG（`img.src`）と WS に cookie が乗らなくなる."""
        (issued,) = _session_cookie_headers(frontend_client.get(f"/m/{MACHINE_ID}/dev"))

        assert "Path=/;" in f"{issued};"

    def test_session_cookie_is_not_rotated_on_the_next_page(
        self, frontend_client: TestClient
    ):
        """ページ遷移ごとに発行し直すと、遷移するたび別人になり操作権が離れる."""
        frontend_client.get(f"/m/{MACHINE_ID}/posctrl")
        first = frontend_client.cookies[SESSION_COOKIE]

        response = frontend_client.get(f"/m/{MACHINE_ID}/pasting")

        assert _session_cookie_headers(response) == []
        assert frontend_client.cookies[SESSION_COOKIE] == first

    def test_proxied_api_response_does_not_issue_a_session_cookie(
        self, echo_frontend: TestClient
    ):
        """発行点は HTML ページだけ（JSON / MJPEG / 静的アセットでは発行しない）."""
        response = echo_frontend.get(f"/m/{MACHINE_ID}/api/state")

        assert _session_cookie_headers(response) == []

    def test_static_asset_does_not_issue_a_session_cookie(
        self, echo_frontend: TestClient
    ):
        response = echo_frontend.get("/static/js/control.js")

        assert response.status_code == 200
        assert _session_cookie_headers(response) == []

    def test_page_session_reaches_the_backend_on_the_next_api_call(
        self, echo_frontend: TestClient
    ):
        """ページで発行した cookie が、そのままプロキシ配下の API 呼び出しに乗る.

        ブラウザ（= cookie jar を持つ TestClient）の視点で、発行・スコープ・ヘッダ翻訳が
        繋がっていることを 1 本で確かめる。`Path` を機体 prefix に狭めるとここで切れる。
        """
        page = echo_frontend.get(f"/m/{MACHINE_ID}/posctrl")
        assert _session_cookie_headers(page) != []
        session = echo_frontend.cookies[SESSION_COOKIE]

        headers = _echoed_headers(echo_frontend.get(f"/m/{MACHINE_ID}/api/state"))

        assert headers["x-pcbasm-session"] == session


class TestSessionHeaderInjection:
    """Cookie → backend ヘッダの翻訳（`ProxyApp` の組み立て 1 箇所）."""

    def test_session_cookie_becomes_the_session_header(self, echo_frontend: TestClient):
        headers = _echoed_headers(
            echo_frontend.get(
                f"/m/{MACHINE_ID}/api/state",
                headers={"cookie": f"{SESSION_COOKIE}=abc123"},
            )
        )

        assert headers["x-pcbasm-session"] == "abc123"

    def test_cookie_is_still_not_forwarded_to_the_backend(
        self, echo_frontend: TestClient
    ):
        """翻訳しても cookie 自体は渡さない（frontend の cookie を漏らさない）."""
        headers = _echoed_headers(
            echo_frontend.get(
                f"/m/{MACHINE_ID}/api/state",
                headers={"cookie": f"{SESSION_COOKIE}=abc123; other=secret"},
            )
        )

        assert "cookie" not in headers

    def test_display_name_stays_percent_encoded_for_the_latin1_header(
        self, echo_frontend: TestClient
    ):
        """日本語名を生で載せると uvicorn / httpx が壊れる（ヘッダは latin-1）."""
        headers = _echoed_headers(
            echo_frontend.get(
                f"/m/{MACHINE_ID}/api/state",
                headers={"cookie": f"{NAME_COOKIE}={QUOTED_NAME}"},
            )
        )

        name = headers["x-pcbasm-client-name"]
        assert name.isascii()
        # 二重エンコードすると backend の unquote 1 回では戻らない
        assert unquote(name) == "田中"

    def test_undecodable_display_name_cookie_is_not_forwarded(
        self, echo_frontend: TestClient
    ):
        """壊れた表示名 cookie はヘッダを付けず backend のフォールバックへ委ねる.

        `unquote` の既定（``errors="replace"``）は壊れた encoding を U+FFFD へ
        「修復」し、quote し直すと**正当な percent-encoding として** backend へ渡る。
        backend の「復元できない値は既定名へ落とす」防御が到達不能になり、文字化け
        1 文字が表示名として全クライアントへ配られる。
        """
        headers = _echoed_headers(
            echo_frontend.get(
                f"/m/{MACHINE_ID}/api/state",
                # encodeURIComponent("田") の途中で切れた cookie（3 バイト中 2 バイト）
                headers={"cookie": f"{NAME_COOKIE}=%E3%81"},
            )
        )

        assert "x-pcbasm-client-name" not in headers

    def test_ascii_display_name_is_passed_through(self, echo_frontend: TestClient):
        headers = _echoed_headers(
            echo_frontend.get(
                f"/m/{MACHINE_ID}/api/state",
                headers={"cookie": f"{NAME_COOKIE}=tanaka"},
            )
        )

        assert headers["x-pcbasm-client-name"] == "tanaka"

    def test_no_cookie_injects_no_session_headers(self, echo_frontend: TestClient):
        """ヘッダ無し = backend 側で anonymous として扱われる（送らないことを確かめる）."""
        headers = _echoed_headers(echo_frontend.get(f"/m/{MACHINE_ID}/api/state"))

        assert "x-pcbasm-session" not in headers
        assert "x-pcbasm-client-name" not in headers

    def test_client_supplied_session_header_is_dropped(self, echo_frontend: TestClient):
        """組み立て点を 1 箇所に保つ（クライアントの自称ヘッダは素通ししない）."""
        headers = _echoed_headers(
            echo_frontend.get(
                f"/m/{MACHINE_ID}/api/state",
                headers={"x-pcbasm-session": "spoofed"},
            )
        )

        assert "x-pcbasm-session" not in headers

    def test_cookie_wins_over_a_client_supplied_header(self, echo_frontend: TestClient):
        headers = _echoed_headers(
            echo_frontend.get(
                f"/m/{MACHINE_ID}/api/state",
                headers={
                    "cookie": f"{SESSION_COOKIE}=from-cookie",
                    "x-pcbasm-session": "spoofed",
                },
            )
        )

        assert headers["x-pcbasm-session"] == "from-cookie"

    def test_forwarding_of_other_headers_is_unchanged(self, echo_frontend: TestClient):
        """セッション注入で `x-forwarded-for` などの既存加工を壊さない."""
        headers = _echoed_headers(
            echo_frontend.get(
                f"/m/{MACHINE_ID}/api/state",
                headers={"accept": "application/json"},
            )
        )

        assert headers["x-forwarded-for"] == "testclient"
        assert headers["accept"] == "application/json"
