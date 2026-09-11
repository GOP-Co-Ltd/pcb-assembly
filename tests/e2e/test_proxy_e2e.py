"""UI frontend のリバースプロキシ経路の E2E（実 uvicorn 2 サーバー・実ソケット経由）.

`tests/web/ui/test_proxy.py` は `ASGITransport` で in-process に組むため、
**終端しない MJPEG と WebSocket は原理的に検証できない**（レスポンスを最後まで
バッファする / websocket scope を扱えない）。この経路の検証点はここにしかない。

最重要は「MJPEG を数バイト読んで切断すると backend の `preview_clients` が 0 に戻る」。
これが固定しているのは**クライアント切断のキャンセルが上流ストリームへ伝播すること**
（キャンセルが `aiter_raw()` へ throw され httpcore が接続を閉じる経路）。プロキシの
明示 `aclose()` は多重防御であり、それを no-op にしても `preview_clients` は 0 に戻る
ので黒箱からは観測できない。一方キャンセル経路を潰す変更ではここが落ちる。

待ちを含むテストはすべて**自前の締め切り**を持たせる（`ws.recv(timeout=…)` /
`httpx` の timeout / `wait_until` / `before_deadline`）。pytest-timeout は
`TestClient` や sync WS の待ちを中断できずハングすることがあり、「守っているつもりで
守っていない」状態になる。

上流を **backend WebAPI ではなく最小 ASGI アプリ**にするテストが 2 つある
（`ui_over_inspection_upstream`）。プロキシが上流へ何を渡したか・上流の close code が
クライアントへ届くか・遅い応答を待てるかは、backend 越しには観測できない。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import attrs
import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from tests.e2e.conftest import (
    TERMINAL as _TERMINAL,
    LiveServer,
    LiveUi,
    drive_job_demo as _drive_job_demo,
    make_api_settings,
    make_ui_settings,
    start_app,
    wait_first_prompt as _wait_first_prompt,
)
from tests.helpers import before_deadline, wait_until
from web.api.app import create_app as create_backend_app
from web.ui.app import create_app as create_ui_app
from web.ui.machines import MachineEndpoint

_HTTP_TIMEOUT = 10.0
_WS_TIMEOUT = 30.0
_WS_OPEN_TIMEOUT = 10.0

# テスト側の締め切り。中継の待ち（1s）と区別できる長さ
_DEADLINE = 15.0

# 検査用上流の設定。ssr_timeout << 上流の遅延 << proxy_read_timeout に置いて、
# 中継が SSR 用の短い既定ではなく proxy_read_timeout で待つことを見る
_SSR_TIMEOUT = 0.2
_UPSTREAM_DELAY = 1.0
_PROXY_READ_TIMEOUT = 10.0

# `/preview/stream` だけ read 無制限であることを見るための設定。
# ssr_timeout(0.2) < proxy_read_timeout(0.5) < 上流の遅延(1.0) なので、read を
# 無制限にしない経路はどちらの値でも打ち切られる
_SHORT_PROXY_READ_TIMEOUT = 0.5

# 上流が使う独自 close code / reason（1000 固定に潰されていないか見る）
_UPSTREAM_CLOSE_CODE = 4001
_UPSTREAM_CLOSE_REASON = "上流の都合で終了しました"


async def _echo_request_over_websocket(websocket: WebSocket) -> None:
    """受信ヘッダと query を JSON で返し、独自 code + reason で閉じる上流."""
    await websocket.accept()
    await websocket.send_json(
        {"headers": dict(websocket.headers), "query": websocket.url.query}
    )
    await websocket.close(code=_UPSTREAM_CLOSE_CODE, reason=_UPSTREAM_CLOSE_REASON)


async def _slow_response(request: Request) -> JSONResponse:
    """`_UPSTREAM_DELAY` 待ってから 200 を返す上流（時間のかかる操作の代役）."""
    await asyncio.sleep(_UPSTREAM_DELAY)
    return JSONResponse({"slept": _UPSTREAM_DELAY})


async def _slow_multipart_frames() -> AsyncIterator[bytes]:
    """フレームの間を `_UPSTREAM_DELAY` 空けて流し続ける（終端しない）."""
    while True:
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\nnot-a-real-jpeg\r\n"
        await asyncio.sleep(_UPSTREAM_DELAY)


async def _slow_multipart_stream(request: Request) -> StreamingResponse:
    """カメラ視聴の代役（実カメラは使わない）。フレーム間隔だけが本物と同じ性質."""
    return StreamingResponse(
        _slow_multipart_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


def _inspection_upstream() -> Starlette:
    """プロキシが渡したものを観測できる最小 ASGI 上流."""
    return Starlette(
        routes=[
            Route("/api/slow", _slow_response),
            Route("/api/preview/stream", _slow_multipart_stream),
            WebSocketRoute("/api/ws", _echo_request_over_websocket),
        ]
    )


def _preview_clients(live_server: LiveServer) -> int:
    """Backend が数えている MJPEG 視聴者数（backend 直で読む）."""
    response = httpx.get(f"{live_server.base_url}/api/state", timeout=_HTTP_TIMEOUT)
    assert response.status_code == 200
    return response.json()["preview_clients"]


def _started_stream(response: httpx.Response) -> Iterator[bytes]:
    """最初のチャンクまで読み進めたイテレータを返す（配信開始を確実にする）.

    イテレータは**呼び出し側が保持し続ける**こと。捨てると GC の `GeneratorExit` が
    httpx の接続解放まで伝わり、まだ読むつもりのストリームが切れる。
    """
    chunks = response.iter_bytes()
    assert next(chunks)
    return chunks


@contextmanager
def _ui_over_inspection_upstream(
    tmp_path: Path, *, proxy_read_timeout: float
) -> Iterator[LiveUi]:
    """検査用上流を 1 台登録した実 frontend を起動する（どちらも実 uvicorn）."""
    upstream = start_app(_inspection_upstream())
    try:
        settings = attrs.evolve(
            make_ui_settings(
                (
                    MachineEndpoint(
                        machine_id="inspect", host="127.0.0.1", port=upstream.port
                    ),
                ),
                machines_file=tmp_path / "absent-machines.toml",
            ),
            ssr_timeout=_SSR_TIMEOUT,
            proxy_read_timeout=proxy_read_timeout,
        )
        frontend = start_app(create_ui_app(settings))
        try:
            yield LiveUi(
                origin=f"http://127.0.0.1:{frontend.port}", machine_ids=("inspect",)
            )
        finally:
            frontend.stop()
    finally:
        upstream.stop()


@pytest.fixture
def ui_over_inspection_upstream(tmp_path: Path) -> Iterator[LiveUi]:
    """検査用上流を 1 台登録した実 frontend（中継の read は上流の遅延より長い）."""
    with _ui_over_inspection_upstream(
        tmp_path, proxy_read_timeout=_PROXY_READ_TIMEOUT
    ) as live_ui:
        yield live_ui


@pytest.fixture
def ui_with_short_read_timeout(tmp_path: Path) -> Iterator[LiveUi]:
    """中継の read を上流の遅延より**短く**した実 frontend.

    この設定では read を無制限にしない経路が必ず打ち切られるので、 `/preview/stream` だけが例外であることを 1
    つの frontend で対照できる。
    """
    with _ui_over_inspection_upstream(
        tmp_path, proxy_read_timeout=_SHORT_PROXY_READ_TIMEOUT
    ) as live_ui:
        yield live_ui


def _wait_terminal_job(ws: Any) -> dict[str, Any]:
    """終端した job_status を受信して返す（recv の締め切り付き）."""
    while True:
        event = json.loads(ws.recv(timeout=_WS_TIMEOUT))
        if event["type"] == "job_status" and event["job"]["status"] in _TERMINAL:
            return event["job"]


class TestPreviewStreamReleasesCamera:
    """MJPEG 中継の切断が backend のカメラ保持を必ず解放する（カメラリークの回帰）.

    固定しているのは**切断のキャンセルが上流ストリームへ伝播すること**。プロキシの
    明示 `aclose()` は多重防御で、黒箱からは観測できない（no-op にしても
    `preview_clients` は 0 に戻る）。
    """

    def test_close_through_proxy_returns_client_count_to_zero(
        self, live_server: LiveServer, live_ui: LiveUi
    ):
        assert _preview_clients(live_server) == 0

        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            with client.stream(
                "GET", f"{live_ui.base_url}/api/preview/stream?overlay=crosshair"
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"] == (
                    "multipart/x-mixed-replace; boundary=frame"
                )
                chunks = response.iter_bytes()
                assert b"--frame" in next(chunks)
                # frontend 経由でも backend は 1 人と数える
                assert _preview_clients(live_server) == 1

        # 切断後のクリーンアップは非同期に走るため短時間ポーリングする。
        # ここが 1 のまま残るのが「切断が上流ストリームへ伝わっていない」状態
        wait_until(
            lambda: _preview_clients(live_server) == 0, timeout=5.0, interval=0.05
        )

    def test_one_viewer_closing_leaves_the_other_streaming(
        self, live_server: LiveServer, live_ui: LiveUi
    ):
        # クライアントは 2 つ作る。同一 httpx.AsyncClient / Client から 2 本 stream
        # すると片方の close で上流が両方終わる（プロキシではなく httpx / uvicorn 側の
        # 性質。1 つで書くと「2 → 1 に減った」が偽陽性で通る）
        stream_url = f"{live_ui.base_url}/api/preview/stream"
        with (
            httpx.Client(timeout=_HTTP_TIMEOUT) as first,
            httpx.Client(timeout=_HTTP_TIMEOUT) as second,
        ):
            with first.stream("GET", stream_url) as kept:
                kept_chunks = _started_stream(kept)
                with second.stream("GET", stream_url) as dropped:
                    dropped_chunks = _started_stream(dropped)
                    assert _preview_clients(live_server) == 2
                    assert next(dropped_chunks)

                wait_until(
                    lambda: _preview_clients(live_server) == 1,
                    timeout=5.0,
                    interval=0.05,
                )
                # 生き残った側は巻き添えで切れていない（追加フレームが届く）
                assert next(kept_chunks)


class TestArtifactsProxy:
    """/artifacts の中継がバイト列と Range を透過する."""

    def test_bytes_and_range_are_transparent(
        self, live_server: LiveServer, live_ui: LiveUi
    ):
        body = "(kicad_pcb (version 20240101))"
        artifact_dir = live_server.settings.webui_data_dir / "proxy-artifact-test"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "board.kicad_pcb").write_text(body, encoding="utf-8")
        url = f"{live_ui.base_url}/artifacts/proxy-artifact-test/board.kicad_pcb"

        full = httpx.get(url, timeout=_HTTP_TIMEOUT)
        partial = httpx.get(url, headers={"Range": "bytes=0-8"}, timeout=_HTTP_TIMEOUT)

        assert full.status_code == 200
        assert full.content == body.encode()
        # ジョブ成果物の再開ダウンロード（206）が中継で 200 に化けない
        assert partial.status_code == 206
        assert partial.content == body.encode()[:9]
        assert partial.headers["content-range"] == f"bytes 0-8/{len(body)}"


class TestJobOverProxiedWebSocket:
    """WS 中継でジョブを起動・応答・中止する（双方向中継の通し検証）."""

    def test_job_demo_completes_with_prompt_answers_through_proxy(
        self, live_ui: LiveUi
    ):
        with connect(f"{live_ui.ws_url}/api/ws", open_timeout=_WS_OPEN_TIMEOUT) as ws:
            response = httpx.post(
                f"{live_ui.base_url}/api/jobs/job_demo",
                json={"params": {"steps": 2, "interval": 0.0}},
                timeout=_HTTP_TIMEOUT,
            )
            assert response.status_code == 201, response.text

            # prompt 応答（クライアント → 上流）とイベント配信（上流 → クライアント）の
            # 両方向が通らないと終端しない
            job, seen_types = _drive_job_demo(ws, number_answer=77.0)

        assert job["status"] == "succeeded"
        assert "77" in job["result"]["summary"]
        assert {"job_status", "log", "progress", "prompt", "prompt_resolved"} <= (
            seen_types
        )

    def test_abort_message_is_relayed_to_backend(self, live_ui: LiveUi):
        with connect(f"{live_ui.ws_url}/api/ws", open_timeout=_WS_OPEN_TIMEOUT) as ws:
            response = httpx.post(
                f"{live_ui.base_url}/api/jobs/job_demo",
                json={"params": {"steps": 1, "interval": 0.0}},
                timeout=_HTTP_TIMEOUT,
            )
            assert response.status_code == 201, response.text
            # prompt 待ち（WAITING_INPUT）で停止しているところへ abort を送る
            _wait_first_prompt(ws)

            ws.send(json.dumps({"type": "abort"}))
            job = _wait_terminal_job(ws)

        assert job["status"] == "aborted"


class TestProxiedWebSocketRequest:
    """WS handshake で上流へ渡すもの / 渡さないものと、close の中継（契約書 §10-3）.

    上流は受信内容を JSON で返す最小 ASGI アプリ。backend WebAPI を上流にすると 「cookie
    を落としたか」「x-forwarded-for を足したか」は応答に現れないため、 落としても足さなくてもテストが素通りする（実測）。
    """

    @staticmethod
    def _open(ui: LiveUi, query: str = "") -> tuple[dict[str, Any], ConnectionClosed]:
        """検査用上流へ WS を張り、受信した JSON と close の情報を返す."""
        with connect(
            f"{ui.ws_url}/api/ws{query}",
            additional_headers={"Cookie": "session=frontend-only"},
            open_timeout=_WS_OPEN_TIMEOUT,
        ) as ws:
            received = json.loads(ws.recv(timeout=_WS_TIMEOUT))
            with pytest.raises(ConnectionClosed) as closed:
                ws.recv(timeout=_WS_TIMEOUT)
        return received, closed.value

    def test_cookie_is_not_forwarded_to_the_backend(
        self, ui_over_inspection_upstream: LiveUi
    ):
        """Frontend のセッション cookie を backend へ漏らさない."""
        received, _closed = self._open(ui_over_inspection_upstream)

        assert "cookie" not in received["headers"]

    def test_client_address_is_forwarded(self, ui_over_inspection_upstream: LiveUi):
        """Backend のログ・監査が中継元ではなく実クライアントを見られるようにする."""
        received, _closed = self._open(ui_over_inspection_upstream)

        assert received["headers"]["x-forwarded-for"] == "127.0.0.1"

    def test_query_string_reaches_the_upstream(
        self, ui_over_inspection_upstream: LiveUi
    ):
        """Query は上流の意味を持つ（job_console.js の購読対象など）."""
        received, _closed = self._open(
            ui_over_inspection_upstream, query="?job=job_demo&tail=5"
        )

        assert received["query"] == "job=job_demo&tail=5"

    def test_upstream_close_code_and_reason_reach_the_client(
        self, ui_over_inspection_upstream: LiveUi
    ):
        """上流の close code / reason をそのままクライアントへ渡す.

        1000 固定に潰すと、クライアントは「正常終了」と解釈して再接続しない
        （`job_console.js` のバックオフ再接続が効かなくなる）。
        """
        _received, closed = self._open(ui_over_inspection_upstream)

        assert closed.rcvd is not None
        assert closed.rcvd.code == _UPSTREAM_CLOSE_CODE
        assert closed.rcvd.reason == _UPSTREAM_CLOSE_REASON


class TestProxyReadTimeout:
    """中継は SSR 用の短い既定ではなく `proxy_read_timeout` で待つ（契約書 6 番）.

    gateway の read timeout は SSR 用（`ssr_timeout`）に短くしてあるので、中継が
    その既定をそのまま使うと `POST /api/machine-control`（M400 待ちで最大 60s）が
    数秒で 504 になる。**この 1 本を落とさないと、中継 timeout を gateway 既定へ
    戻す変更が全テスト素通りする**（実測）。
    """

    def test_slow_upstream_response_is_relayed_instead_of_timing_out(
        self, ui_over_inspection_upstream: LiveUi
    ):
        url = f"{ui_over_inspection_upstream.base_url}/api/slow"

        response = before_deadline(
            lambda: httpx.get(url, timeout=_HTTP_TIMEOUT), deadline=_DEADLINE
        )

        assert response.status_code == 200, response.text
        assert response.json()["slept"] == _UPSTREAM_DELAY


class TestPreviewStreamIsExemptFromReadTimeout:
    """`/preview/stream` だけ read 無制限（他の経路は打ち切る）.

    カメラ視聴はフレーム間隔が空くだけで終端しないため、read を効かせると
    `proxy_read_timeout`（既定 120s）を超えた視聴が無言で切れる。この性質は
    `TestProxyReadTimeout` では守れない（そこは read が**長い**ことしか見ておらず、
    `/preview/stream` の例外を潰しても素通りする）。
    """

    def test_stream_survives_gaps_longer_than_the_read_timeout(
        self, ui_with_short_read_timeout: LiveUi
    ):
        """フレーム間隔（1.0s）> 中継の read（0.5s）でも 2 フレーム目が届く."""
        url = f"{ui_with_short_read_timeout.base_url}/api/preview/stream"

        def read_two_frames() -> bytes:
            with (
                httpx.Client(timeout=_HTTP_TIMEOUT) as client,
                client.stream("GET", url) as response,
            ):
                assert response.status_code == 200, response.read()
                received = b""
                for chunk in response.iter_bytes():
                    received += chunk
                    if received.count(b"--frame") >= 2:
                        return received
                # 打ち切られるとイテレータが例外か終端で抜ける（ハングしない）
                return pytest.fail(f"2 フレーム目が届きませんでした: {received!r}")

        received = before_deadline(read_two_frames, deadline=_DEADLINE)

        assert received.count(b"--frame") >= 2

    def test_other_paths_are_still_cut_off_by_the_read_timeout(
        self, ui_with_short_read_timeout: LiveUi
    ):
        """同じ設定でも `/preview/stream` 以外は 504（例外がこの 1 経路に限られる）."""
        url = f"{ui_with_short_read_timeout.base_url}/api/slow"

        response = before_deadline(
            lambda: httpx.get(url, timeout=_HTTP_TIMEOUT), deadline=_DEADLINE
        )

        assert response.status_code == 504, response.text


class TestWebSocketHandshakeRejection:
    """繋げない WS は accept せず handshake を拒否する（再接続バックオフが効く）."""

    def test_unknown_machine_id_is_rejected_before_accept(self, live_ui: LiveUi):
        with pytest.raises(InvalidStatus) as rejected:
            with connect(
                f"{live_ui.ws_origin}/m/nosuch/api/ws", open_timeout=_WS_OPEN_TIMEOUT
            ):
                pass

        assert rejected.value.response.status_code == 403

    def test_unreachable_backend_is_rejected_before_accept(self, tmp_path: Path):
        # 登録済みだが誰も listen していない port（実 ECONNREFUSED）
        frontend = start_app(
            create_ui_app(
                make_ui_settings(
                    (MachineEndpoint(machine_id="dead", host="127.0.0.1", port=1),),
                    machines_file=tmp_path / "absent-machines.toml",
                )
            )
        )
        try:
            with pytest.raises(InvalidStatus) as rejected:
                with connect(
                    f"ws://127.0.0.1:{frontend.port}/m/dead/api/ws",
                    open_timeout=_WS_OPEN_TIMEOUT,
                ):
                    pass
        finally:
            frontend.stop()

        assert rejected.value.response.status_code == 403


class TestBackendShutdownClosesProxiedWebSocket:
    """Backend が落ちたら中継 WS もクライアント側で閉じる（無言で生き残らない）."""

    def test_client_websocket_closes_when_backend_stops(self, tmp_path: Path):
        # backend を途中で殺すため、この 1 本だけ専用の 2 サーバーを立てる
        backend = start_app(
            create_backend_app(make_api_settings(tmp_path / "dying", hostname="dying"))
        )
        try:
            frontend = start_app(
                create_ui_app(
                    make_ui_settings(
                        (
                            MachineEndpoint(
                                machine_id="dying",
                                host="127.0.0.1",
                                port=backend.port,
                            ),
                        ),
                        machines_file=tmp_path / "absent-machines.toml",
                    )
                )
            )
            try:
                with connect(
                    f"ws://127.0.0.1:{frontend.port}/m/dying/api/ws",
                    open_timeout=_WS_OPEN_TIMEOUT,
                ) as ws:
                    # 未知 type は error イベントで返る = 双方向に繋がっている証跡
                    ws.send(json.dumps({"type": "__liveness__"}))
                    assert json.loads(ws.recv(timeout=_WS_TIMEOUT))["type"] == "error"

                    backend.stop()

                    with pytest.raises(ConnectionClosed):
                        ws.recv(timeout=_WS_TIMEOUT)
            finally:
                frontend.stop()
        finally:
            backend.stop()
