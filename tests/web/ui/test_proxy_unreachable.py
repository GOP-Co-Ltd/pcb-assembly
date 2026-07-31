"""到達できない / 応答しない backend に対する `web.ui.proxy.ProxyApp` の振る舞い.

契約書 /tmp/pcbasm-plan/mr4-interface-contract.md「トラック A」のエラー規約:
`ConnectError` / `ConnectTimeout` → 502、`ReadTimeout` → 504、いずれも
`{"detail": …}` 形（`app.js` の既存トーストがそのまま読める形）。

**上流は実ソケットを使う（`MockTransport` を使わない）。** transport を差し替えると
「httpx が投げると決めた例外」しか試せず、実際に起きる ECONNREFUSED や無応答が
どの例外になるかを検証できない。

- 502: 誰も listen していない port（`127.0.0.1:1`）へ実接続して ECONNREFUSED
- 504: accept だけして何も返さないソケットへ実接続（read timeout）

MJPEG（`read=None`）と WS の中継は in-process では検証できない（`ASGITransport` は
レスポンスをバッファし websocket scope も扱えない）ため `tests/e2e/test_proxy_e2e.py`
で実 uvicorn（実ソケット経由）で確かめる。
"""

import socket
import threading
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from web.ui.machine_client import BackendGateway
from web.ui.machines import MachineEndpoint, MachineRegistry
from web.ui.proxy import ProxyApp

# 応答を待つ上限。実運用の既定（120s）ではテストが終わらないので短くする
READ_TIMEOUT = 0.5

# テスト側の締め切り。READ_TIMEOUT より十分長く、ハングと区別できる長さ
DEADLINE = 15.0


def _client(endpoint: MachineEndpoint) -> TestClient:
    """実 TCP で `endpoint` へ中継する frontend（transport は差し替えない）.

    `with` で使わない（アプリに lifespan が無く、応答が返らないときに
    `TestClient.__exit__` が in-flight のリクエストを待って共倒れになる）。
    """
    gateway = BackendGateway(connect_timeout=1.0, read_timeout=READ_TIMEOUT)
    app = Starlette(
        routes=[
            Mount(
                "/m/{machine_id}/api",
                ProxyApp(
                    MachineRegistry((endpoint,)),
                    gateway,
                    target_prefix="/api",
                    read_timeout=READ_TIMEOUT,
                ),
            )
        ]
    )
    return TestClient(app)


def _get_before_deadline(client: TestClient, path: str) -> httpx.Response:
    """`DEADLINE` 以内に返らなければ失敗させる GET.

    プロキシが read timeout を効かせていないと応答は永久に来ない。ハングは
    「テストが落ちる」ではなく「テストが終わらない」なので、締め切りをテスト側に
    持たせて失敗に変える（`pytest.mark.timeout` は TestClient の待ちを中断できなかった）。
    返らなかったスレッドは daemon にして放置する。
    """
    received: list[httpx.Response] = []
    request = threading.Thread(
        target=lambda: received.append(client.get(path)), daemon=True
    )
    request.start()
    request.join(DEADLINE)
    if not received:
        pytest.fail(f"{DEADLINE}s 以内に応答が返りませんでした: {path}")
    return received[0]


@pytest.fixture
def blackhole_port() -> Iterator[int]:
    """接続は受けるが何も返さないソケットの port（read timeout の材料）."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    # accept しなくても backlog の範囲で TCP handshake は完了する
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


class TestUnreachableBackend:
    """接続できない backend."""

    def test_connection_refused_returns_502_json(self):
        endpoint = MachineEndpoint(machine_id="dead", host="127.0.0.1", port=1)

        response = _get_before_deadline(_client(endpoint), "/m/dead/api/state")

        assert response.status_code == 502
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["detail"]


class TestUnresponsiveBackend:
    """接続はできるが応答しない backend."""

    def test_no_response_returns_504_json(self, blackhole_port: int):
        endpoint = MachineEndpoint(
            machine_id="mute", host="127.0.0.1", port=blackhole_port
        )

        response = _get_before_deadline(_client(endpoint), "/m/mute/api/state")

        assert response.status_code == 504
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["detail"]
