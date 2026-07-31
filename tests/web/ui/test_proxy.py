"""`web.ui.proxy.ProxyApp` の仕様テスト.

契約書 /tmp/pcbasm-plan/mr4-interface-contract.md「トラック A」が契約:

- `Mount("/m/{machine_id}/<prefix>", ProxyApp(...))` が method / body / query /
  status / ヘッダを素通しする
- リクエストヘッダは `host` / hop-by-hop / `cookie` を落として `x-forwarded-for` を足す
- レスポンスヘッダは hop-by-hop と `date` / `server` を落とし、多値ヘッダは潰さない
- 未知の machine_id は 404

**MJPEG と WS の in-process テストは書かない。** `ASGITransport` はレスポンスを
最後までバッファしてから返すので終端しない multipart を読むと永久にハングし、
websocket scope も扱えない（backend の lifespan も走らないのでジョブ実行と WS
イベント配信自体が成立しない）。上流を閉じ忘れてカメラが回り続ける回帰、WS の
中継・close code、read timeout の 504 は実 uvicorn（実ソケット経由）で検証する
（`tests/e2e/test_proxy_e2e.py` と `tests/web/ui/test_proxy_unreachable.py`）。

上流は 2 種類を使い分ける:

- **実 backend app**（`backend_app`）: 実際の API 契約が通ることの確認
- **エコー上流**（`_echo_upstream`）: ヘッダ加工の確認。`date` / `server` / 多値
  `set-cookie` は uvicorn が付けるヘッダなので in-process の実 backend では観測できず、
  「落としている」ことを上流から送って確かめる必要がある
"""

import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from tests.helpers import TESTING_DATA_DIR
from web.api.settings import Settings as ApiSettings
from web.ui.machine_client import BackendGateway
from web.ui.machines import MachineEndpoint, MachineRegistry
from web.ui.proxy import ProxyApp
from web.ui.settings import Settings

# tests/web/ui/conftest.py の静的登録 1 台と同じ id
MACHINE_ID = "uitest"
REAL_PCB = TESTING_DATA_DIR / "fill_coverage" / "fill_coverage.kicad_pcb"

# エコー上流が必ず返すヘッダ。プロキシが落とす / 残すことを確かめる材料
_ECHO_STATUS = 201
_ECHO_RESPONSE_HEADERS = [
    (b"content-type", b"application/json"),
    (b"date", b"Mon, 01 Jan 2035 00:00:00 GMT"),
    (b"server", b"upstream-server"),
    (b"connection", b"keep-alive"),
    (b"transfer-encoding", b"chunked"),
    (b"set-cookie", b"first=1; Path=/"),
    (b"set-cookie", b"second=2; Path=/"),
    (b"x-upstream-note", b"kept"),
]


async def _echo_upstream(scope: Scope, receive: Receive, send: Send) -> None:
    """受け取ったリクエストを JSON で返す上流 ASGI アプリ."""
    body = b""
    more_body = True
    while more_body:
        message = await receive()
        body += message.get("body", b"")
        more_body = message.get("more_body", False)
    payload = json.dumps(
        {
            "method": scope["method"],
            "path": scope["path"],
            "query": scope["query_string"].decode("latin-1"),
            "headers": {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope["headers"]
            },
            "body": body.decode("utf-8"),
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": _ECHO_STATUS,
            "headers": _ECHO_RESPONSE_HEADERS,
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _proxy_client(
    machines: tuple[MachineEndpoint, ...],
    transport: httpx.AsyncBaseTransport,
) -> TestClient:
    """`/api` と `/artifacts` を中継する最小の frontend（app.py 相当の配線）.

    `web.ui.app.create_app` を使わずここで mount するのは、プロキシ単体の仕様を
    アプリ全体の登録順（`/m/{machine_id}/{tab}` との競合）と切り離すため。
    登録順そのものは `tests/web/ui/test_pages.py` と e2e が押さえる。
    """
    registry = MachineRegistry(machines)
    settings = Settings()
    gateway = BackendGateway(
        connect_timeout=settings.backend_connect_timeout,
        # SSR 用の短い read timeout。プロキシがこれを使い回さないことは
        # test_proxy_unreachable.py 側で確かめる
        read_timeout=settings.ssr_timeout,
        transport_factory=lambda _endpoint: transport,
    )
    app = Starlette(
        routes=[
            Mount(
                f"/m/{{machine_id}}/{prefix}",
                ProxyApp(
                    registry,
                    gateway,
                    target_prefix=f"/{prefix}",
                    read_timeout=settings.proxy_read_timeout,
                ),
            )
            for prefix in ("api", "artifacts")
        ]
    )
    return TestClient(app)


@pytest.fixture
def echo_client(ui_settings: Settings) -> Iterator[TestClient]:
    """エコー上流を挿した frontend."""
    with _proxy_client(
        ui_settings.machines, httpx.ASGITransport(app=_echo_upstream)
    ) as client:
        yield client


@pytest.fixture
def backend_client(ui_settings: Settings, backend_app: FastAPI) -> Iterator[TestClient]:
    """実 backend app を挿した frontend."""
    with _proxy_client(
        ui_settings.machines, httpx.ASGITransport(app=backend_app)
    ) as client:
        yield client


def _echoed(response: httpx.Response) -> dict:
    assert response.status_code == _ECHO_STATUS, response.text
    return response.json()


class TestRelayedRequest:
    """Method / body / query / パス prefix の素通し."""

    @pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "POST", "DELETE"])
    def test_method_reaches_upstream(self, echo_client: TestClient, method: str):
        response = echo_client.request(method, f"/m/{MACHINE_ID}/api/state")

        assert _echoed(response)["method"] == method

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "POST"])
    def test_request_body_reaches_upstream(self, echo_client: TestClient, method: str):
        response = echo_client.request(
            method, f"/m/{MACHINE_ID}/api/state", json={"path": "boards/a.kicad_pcb"}
        )

        echoed = _echoed(response)
        assert json.loads(echoed["body"]) == {"path": "boards/a.kicad_pcb"}
        # ボディ長は上流にもそのまま伝える（chunked に化けさせない）
        assert echoed["headers"]["content-length"] == str(len(echoed["body"]))
        assert "transfer-encoding" not in echoed["headers"]

    def test_bodyless_request_is_not_chunked(self, echo_client: TestClient):
        """GET を chunked にすると上流が 400 を返すことがある."""
        echoed = _echoed(echo_client.get(f"/m/{MACHINE_ID}/api/state"))

        assert "transfer-encoding" not in echoed["headers"]
        assert "content-length" not in echoed["headers"]

    def test_query_string_is_reattached(self, echo_client: TestClient):
        """`/api/preview/stream?overlay=…` の query を落とすと overlay が効かない."""
        response = echo_client.get(
            f"/m/{MACHINE_ID}/api/preview/stream",
            params={"overlay": "canny", "canny_low": 12.5},
        )

        echoed = _echoed(response)
        assert echoed["path"] == "/api/preview/stream"
        assert echoed["query"] == "overlay=canny&canny_low=12.5"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            (f"/m/{MACHINE_ID}/api/", "/api/"),
            (f"/m/{MACHINE_ID}/api/jobs/current", "/api/jobs/current"),
            (f"/m/{MACHINE_ID}/artifacts/job-1/plot.png", "/artifacts/job-1/plot.png"),
        ],
    )
    def test_target_prefix_replaces_machine_prefix(
        self, echo_client: TestClient, path: str, expected: str
    ):
        assert _echoed(echo_client.get(path))["path"] == expected


class TestRelayedRequestHeaders:
    """上流へ渡すリクエストヘッダの加工."""

    def test_cookie_is_not_leaked_to_backend(self, echo_client: TestClient):
        """Frontend のセッション cookie は backend に渡さない."""
        response = echo_client.get(
            f"/m/{MACHINE_ID}/api/state", headers={"cookie": "session=secret"}
        )

        assert "cookie" not in _echoed(response)["headers"]

    @pytest.mark.parametrize(
        ("header", "value"),
        [
            ("te", "trailers"),  # codespell:ignore te
            ("upgrade", "h2c"),
            ("keep-alive", "timeout=1"),
        ],
    )
    def test_hop_by_hop_request_headers_are_dropped(
        self, echo_client: TestClient, header: str, value: str
    ):
        response = echo_client.get(
            f"/m/{MACHINE_ID}/api/state", headers={header: value}
        )

        assert header not in _echoed(response)["headers"]

    def test_client_connection_header_does_not_reach_backend(
        self, echo_client: TestClient
    ):
        """`connection: close` を渡すと上流の keep-alive 接続が毎回切られる.

        `connection` は httpx 自身が接続管理として付け直すため、上流に届く値は
        クライアントの値ではなく httpx の keep-alive になる。
        """
        response = echo_client.get(
            f"/m/{MACHINE_ID}/api/state", headers={"connection": "close"}
        )

        assert _echoed(response)["headers"]["connection"] == "keep-alive"

    def test_host_becomes_the_backend_host(self, echo_client: TestClient):
        """クライアントの Host を渡すと上流の名前解決・ログが frontend 側になる."""
        response = echo_client.get(f"/m/{MACHINE_ID}/api/state")

        assert _echoed(response)["headers"]["host"] == "127.0.0.1:8081"

    def test_x_forwarded_for_carries_the_client_address(self, echo_client: TestClient):
        response = echo_client.get(f"/m/{MACHINE_ID}/api/state")

        assert _echoed(response)["headers"]["x-forwarded-for"] == "testclient"

    def test_existing_x_forwarded_for_is_appended_to(self, echo_client: TestClient):
        response = echo_client.get(
            f"/m/{MACHINE_ID}/api/state", headers={"x-forwarded-for": "203.0.113.9"}
        )

        forwarded = _echoed(response)["headers"]["x-forwarded-for"]
        assert forwarded == "203.0.113.9, testclient"

    def test_other_request_headers_are_preserved(self, echo_client: TestClient):
        response = echo_client.get(
            f"/m/{MACHINE_ID}/api/state", headers={"accept": "application/json"}
        )

        assert _echoed(response)["headers"]["accept"] == "application/json"


class TestRelayedResponseHeaders:
    """クライアントへ返すレスポンスヘッダの加工."""

    @pytest.mark.parametrize("header", ["date", "server"])
    def test_uvicorn_default_headers_are_dropped(
        self, echo_client: TestClient, header: str
    ):
        """Uvicorn は date / server を無条件に prepend するので上流の分を残すと 2 つ並ぶ."""
        response = echo_client.get(f"/m/{MACHINE_ID}/api/state")

        assert header not in response.headers

    @pytest.mark.parametrize("header", ["connection", "transfer-encoding"])
    def test_hop_by_hop_response_headers_are_dropped(
        self, echo_client: TestClient, header: str
    ):
        response = echo_client.get(f"/m/{MACHINE_ID}/api/state")

        assert header not in response.headers

    def test_multi_valued_response_headers_survive(self, echo_client: TestClient):
        """Dict 化すると set-cookie が 1 本に潰れる."""
        response = echo_client.get(f"/m/{MACHINE_ID}/api/state")

        assert response.headers.get_list("set-cookie") == [
            "first=1; Path=/",
            "second=2; Path=/",
        ]

    def test_other_response_headers_are_preserved(self, echo_client: TestClient):
        response = echo_client.get(f"/m/{MACHINE_ID}/api/state")

        assert response.headers["x-upstream-note"] == "kept"
        assert response.headers["content-type"] == "application/json"

    def test_upstream_status_is_preserved(self, echo_client: TestClient):
        response = echo_client.get(f"/m/{MACHINE_ID}/api/state")

        assert response.status_code == _ECHO_STATUS


class TestRelayToRealBackend:
    """実 backend app を上流にした素通し."""

    def test_get_reaches_the_registered_backend(self, backend_client: TestClient):
        """自己申告 machine_id が URL の machine_id と一致する backend に届く."""
        response = backend_client.get(f"/m/{MACHINE_ID}/api/machine-info")

        assert response.status_code == 200, response.text
        assert response.json()["machine_id"] == MACHINE_ID

    def test_put_pcb_file_selects_on_backend(self, backend_client: TestClient):
        response = backend_client.put(
            f"/m/{MACHINE_ID}/api/pcb-file", json={"path": "boards/sample.kicad_pcb"}
        )

        assert response.status_code == 200, response.text
        assert response.json()["pcb_file"] == "boards/sample.kicad_pcb"

    def test_multipart_upload_lands_in_backend_upload_dir(
        self, backend_client: TestClient, backend_settings: ApiSettings
    ):
        """実 .kicad_pcb をプロキシ経由で送り、bytes が一致することを確かめる."""
        with REAL_PCB.open("rb") as pcb:
            response = backend_client.post(
                f"/m/{MACHINE_ID}/api/pcb-file/upload",
                files={"file": ("uploaded.kicad_pcb", pcb, "application/octet-stream")},
            )

        assert response.status_code == 201, response.text
        uploaded = backend_settings.pcb_upload_dir / "uploaded.kicad_pcb"
        assert uploaded.read_bytes() == REAL_PCB.read_bytes()

    def test_backend_conflict_is_relayed_with_owner(
        self, backend_client: TestClient, backend_app: FastAPI
    ):
        """409 の `{detail, owner}` が潰れると app.js のトーストが owner を出せない."""
        with backend_app.state.appstate.machine_lock("pytest-job"):
            response = backend_client.put(
                f"/m/{MACHINE_ID}/api/pcb-file",
                json={"path": "boards/sample.kicad_pcb"},
            )

        assert response.status_code == 409
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["owner"] == "pytest-job"

    def test_artifacts_are_relayed_as_bytes(
        self, backend_client: TestClient, backend_settings: ApiSettings
    ):
        artifact = backend_settings.webui_data_dir / "job-1" / "plot.png"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-png")

        response = backend_client.get(f"/m/{MACHINE_ID}/artifacts/job-1/plot.png")

        assert response.status_code == 200, response.text
        assert response.content == artifact.read_bytes()


class TestUnknownMachine:
    """未登録の machine_id."""

    def test_unknown_machine_id_returns_404_json(self, echo_client: TestClient):
        response = echo_client.get("/m/nosuchmachine/api/state")

        assert response.status_code == 404
        assert "nosuchmachine" in response.json()["detail"]

    def test_known_machine_is_resolved_by_id_not_by_position(self):
        """複数登録では URL の machine_id に対応する backend へ中継する."""
        machines = (
            MachineEndpoint(machine_id="alpha", host="10.0.0.1", port=8081),
            MachineEndpoint(machine_id="bravo", host="10.0.0.2", port=8081),
        )
        with _proxy_client(machines, httpx.ASGITransport(app=_echo_upstream)) as client:
            response = client.get("/m/bravo/api/state")

        assert _echoed(response)["headers"]["host"] == "10.0.0.2:8081"
