"""``/m/{id}/api`` と ``/m/{id}/artifacts`` を backend へ中継するプロキシ.

FastAPI のルートではなく純 ASGI アプリにしている:

- ボディが依存解決（`Request` の body 読み込み）に巻き込まれず、そのまま上流へ
  ストリームできる（MJPEG とアップロードの両方で必要）
- http と websocket を 1 つの ``Mount`` で扱える

``Mount("/m/{machine_id}/api", ProxyApp(...))`` は http / websocket の両方で
``scope["path_params"]`` を埋め、``root_path`` に prefix を入れて ``path`` は全体を残す。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, override
from urllib.parse import quote, unquote

import anyio
import httpx
import websockets
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketState

from web.ui.machines import MachineEndpoint, MachineRegistry, UnknownMachine

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.datastructures import Headers

    from web.ui.machine_client import BackendGateway

logger = logging.getLogger(__name__)

# RFC 9110 の hop-by-hop ヘッダ。中継先へ渡すと接続管理が二重に解釈される
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",  # codespell:ignore te
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# 操作権リースのセッション同定に使う cookie（発行は `web.ui.pages` の HTML 応答だけ）。
# `pcbasm_name` は JS が読み書きする表示名（httpOnly にしない）
SESSION_COOKIE = "pcbasm_session"
NAME_COOKIE = "pcbasm_name"

# cookie から翻訳して backend へ渡すヘッダ（`web.api` の `get_identity` が読む）
_SESSION_HEADER = b"x-pcbasm-session"
_NAME_HEADER = b"x-pcbasm-client-name"

# host は上流の URL から httpx / websockets が付け直す。
# cookie は frontend のセッション cookie を backend に漏らさないために落とす
# （落とす前に読んでセッションヘッダへ翻訳する = `_session_headers`）。
# セッションヘッダはクライアントが付けた値を素通しせず、必ず cookie から組み直す
# （組み立て点を 1 箇所に保つ）
_DROP_REQUEST_HEADERS = (
    _HOP_BY_HOP
    | {"host", "cookie"}
    | {_SESSION_HEADER.decode("ascii"), _NAME_HEADER.decode("ascii")}
)

# websockets が handshake で自前に組むヘッダ。そのまま渡すと重複して壊れる
# （sec-websocket-protocol は scope["subprotocols"] 経由で渡す）
_DROP_WEBSOCKET_HEADERS = _DROP_REQUEST_HEADERS | {
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
}

# date / server は uvicorn が default_headers として無条件に prepend するため、
# 上流の分を残すと応答に 2 つ並ぶ
_DROP_RESPONSE_HEADERS = _HOP_BY_HOP | {"date", "server"}

# 終端しない MJPEG。この経路だけ read timeout を外す（他は read_timeout で打ち切る）
_UNBOUNDED_STREAM_PATHS = frozenset({"/preview/stream"})

# 中継の失敗と、送信できない close code（1005 = 未受信 / 1006 = 異常終了）の丸め先
_RELAY_FAILURE_CLOSE_CODE = 1011
_UNSENDABLE_CLOSE_CODES = frozenset({1005, 1006})


class ProxyApp:
    """1 つの prefix（``/api`` または ``/artifacts``）を中継する ASGI アプリ."""

    def __init__(
        self,
        registry: MachineRegistry,
        gateway: BackendGateway,
        *,
        target_prefix: str,
        read_timeout: float,
    ) -> None:
        """プロキシを初期化する.

        Args:
            registry: machine_id → エンドポイントの解決に使う registry
            gateway: マシン毎の `httpx.AsyncClient` を持つ gateway
            target_prefix: 上流のパス prefix（``"/api"`` / ``"/artifacts"``）
            read_timeout: 上流の応答を待つ上限 [s]（``Settings.proxy_read_timeout``）。
                gateway の既定は SSR 用の短い値なので中継では使えない
                （`POST /api/machine-control` は M400 待ちで最大 60s かかる）
        """
        self._registry = registry
        self._gateway = gateway
        self._target_prefix = target_prefix
        self._read_timeout = read_timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """リクエストを上流へ中継する（http と websocket の両方を受ける）."""
        if scope["type"] == "websocket":
            await self._relay_websocket(scope, receive, send)
            return
        await self._relay_http(scope, receive, send)

    async def _relay_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            endpoint = self._registry.resolve(scope["path_params"]["machine_id"])
        except UnknownMachine as exc:
            await _error_response(404, str(exc))(scope, receive, send)
            return

        request = Request(scope, receive)
        route_path = _route_path(scope)
        url = self._target_url(endpoint, route_path, scope["query_string"])
        client = self._gateway.client_for(endpoint)
        upstream_request = client.build_request(
            request.method,
            url,
            headers=_forward_headers(request, _DROP_REQUEST_HEADERS),
            # ボディは pull chain で流す（1 チャンクしかメモリに載らず
            # バックプレッシャが効く）。ボディが無いメソッドで content を渡すと
            # httpx が GET に chunked を付けてしまうため、ある時だけ渡す
            content=request.stream() if _has_body(request.headers) else None,
            timeout=self._relay_timeout(client.timeout, route_path),
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.warning("backend に接続できません: %s: %s", url, exc)
            await _error_response(502, f"backend に接続できません: {exc}")(
                scope, receive, send
            )
            return
        except httpx.ReadTimeout as exc:
            logger.warning("backend の応答がありません: %s: %s", url, exc)
            await _error_response(504, f"backend の応答がありません: {exc}")(
                scope, receive, send
            )
            return
        except httpx.HTTPError as exc:
            logger.warning("backend への中継に失敗しました: %s: %s", url, exc)
            await _error_response(502, f"backend への中継に失敗しました: {exc}")(
                scope, receive, send
            )
            return
        await _UpstreamResponse(upstream)(scope, receive, send)

    async def _relay_websocket(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        # handshake 要求を受け取ってから上流へ繋ぐ。accept は接続成功後に行う
        # （失敗を accept 済みの close で伝えると、クライアントは「一度は繋がった」と
        # 判断してしまう。accept しなければ job_console.js のバックオフ再接続が効く）
        await websocket.receive()
        try:
            endpoint = self._registry.resolve(scope["path_params"]["machine_id"])
        except UnknownMachine as exc:
            logger.warning("未知の machine_id への WS: %s", exc)
            await websocket.close(code=_RELAY_FAILURE_CLOSE_CODE)
            return

        route_path = _route_path(scope)
        url = _websocket_target_url(
            endpoint, f"{self._target_prefix}{route_path}", scope["query_string"]
        )
        try:
            upstream = await websockets.connect(
                url,
                additional_headers=_forward_websocket_headers(websocket),
                subprotocols=scope.get("subprotocols") or None,
                # 既定の proxy=True は HTTP_PROXY / ALL_PROXY を読むため、proxy env の
                # ある環境で LAN 内の backend への WS が全滅する
                proxy=None,
            )
        except (OSError, TimeoutError, websockets.WebSocketException) as exc:
            logger.warning("上流 WS に接続できません: %s: %s", url, exc)
            await websocket.close(code=_RELAY_FAILURE_CLOSE_CODE)
            return

        try:
            await websocket.accept(subprotocol=upstream.subprotocol)
            await _pump_websocket(websocket, upstream)
            if websocket.client_state is WebSocketState.CONNECTED:
                await websocket.close(
                    code=_relayed_close_code(upstream.close_code),
                    reason=upstream.close_reason or "",
                )
        finally:
            # 上流を閉じ残すと backend 側の購読が残る。キャンセル中でも完遂させる
            with anyio.CancelScope(shield=True):
                await upstream.close()

    def _relay_timeout(self, base: httpx.Timeout, route_path: str) -> httpx.Timeout:
        """Gateway の既定（SSR 用の短い read）を中継用に差し替えた Timeout.

        MJPEG は終端しないので read を外す。write（上流へボディを流す時間）も SSR の
        既定では短すぎるので中継側の値を使う（PCB ファイルのアップロード）。
        """
        return httpx.Timeout(
            connect=base.connect,
            read=None if route_path in _UNBOUNDED_STREAM_PATHS else self._read_timeout,
            write=self._read_timeout,
            pool=base.pool,
        )

    def _target_url(
        self, endpoint: MachineEndpoint, route_path: str, query_string: bytes
    ) -> httpx.URL:
        url = f"{endpoint.base_url}{self._target_prefix}{route_path}"
        if query_string:
            # query は上流の意味を持つ（例: /api/preview/stream?overlay=…）ので
            # そのまま付け直す
            url = f"{url}?{query_string.decode('latin-1')}"
        return httpx.URL(url)


class _UpstreamResponse(StreamingResponse):
    """上流の `httpx.Response` をバイト列のまま流し、明示的に close する応答.

    上流を開いたまま残すと別プロセスの backend が MJPEG を送り続け、`hold_camera` の
    finally が走らずカメラが永久に回る。実際の解放を担っているのは**クライアント切断の
    キャンセルが `aiter_raw()` へ伝播する経路**（httpcore が閉じる）で、ここでの
    `aclose()` は**多重防御**にあたる（no-op にしても backend の `preview_clients` は
    0 に戻るため、黒箱からは観測できない。`tests/e2e/test_proxy_e2e.py` で実測）。
    starlette はキャンセル時に body_iterator を閉じないので、`stream_response` の
    finally（正常終了・送信中エラー）と `__call__` の `CancelledError`（サーバー側からの
    キャンセル）の両方に置く。
    """

    def __init__(self, upstream: httpx.Response) -> None:
        """上流応答をラップする.

        Args:
            upstream: `stream=True` で送信済みの上流応答
        """
        # aiter_raw: content-encoding を解かない生バイト列。上流の
        # content-length / content-encoding をそのまま転送できる
        # （multipart も再解析せず素通しになる）
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code)
        self._upstream = upstream
        # 多値ヘッダ（set-cookie 等）を潰さないため dict を経由せず raw を差し替える
        self.raw_headers = [
            (key, value)
            for key, value in upstream.headers.raw
            if key.decode("latin-1").lower() not in _DROP_RESPONSE_HEADERS
        ]

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            await self._close_upstream()
            raise

    @override
    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            await self._close_upstream()

    async def _close_upstream(self) -> None:
        # shield なしではキャンセル中の await が即座に再キャンセルされ、上流が
        # 開いたまま残る
        with anyio.CancelScope(shield=True):
            await self._upstream.aclose()


async def _pump_websocket(
    websocket: WebSocket, upstream: websockets.ClientConnection
) -> None:
    """クライアント ⇄ 上流のフレームを双方向に中継する.

    1 ブラウザ = 1 上流 WS（fan-out で共有しない。共有すると操作権をセッションに
    帰属させられなくなる）。どちらかの向きが終わったら中継全体を畳む。
    """
    tasks = (
        asyncio.create_task(_forward_client_frames(websocket, upstream)),
        asyncio.create_task(_forward_upstream_frames(upstream, websocket)),
    )
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _forward_client_frames(
    websocket: WebSocket, upstream: websockets.ClientConnection
) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        text = message.get("text")
        if text is not None:
            await upstream.send(text)
            continue
        data = message.get("bytes")
        if data is not None:
            await upstream.send(data)


async def _forward_upstream_frames(
    upstream: websockets.ClientConnection, websocket: WebSocket
) -> None:
    try:
        async for frame in upstream:
            if isinstance(frame, str):
                await websocket.send_text(frame)
            else:
                await websocket.send_bytes(frame)
    except websockets.ConnectionClosed:
        return


def _relayed_close_code(close_code: int | None) -> int:
    """上流の close code をクライアントへ送れる code に丸める.

    1005（close frame 無し）と 1006（異常終了）は実際に送られた code ではないため
    中継できない。未取得（None）も含めて 1011 にする。
    """
    if close_code is None or close_code in _UNSENDABLE_CLOSE_CODES:
        return _RELAY_FAILURE_CLOSE_CODE
    return close_code


def _route_path(scope: Scope) -> str:
    """``Mount`` の prefix（``root_path``）を除いた残りのパス.

    `starlette._utils.get_route_path` の移植。`starlette.routing` 経由の import が
    private import として型エラーになるため参照しない（`starlette._utils` は private
    モジュールなのでそちらも import しない）。
    """
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if not root_path:
        return path
    if not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    # root_path に前方一致するがセグメント境界ではない場合は切り詰めない。
    # Mount の path_regex が境界に "/" を要求するため、この分岐には到達しない
    return path


def _websocket_target_url(
    endpoint: MachineEndpoint, path: str, query_string: bytes
) -> str:
    url = f"ws://{endpoint.host}:{endpoint.port}{path}"
    if query_string:
        url = f"{url}?{query_string.decode('latin-1')}"
    return url


def _forward_headers(
    connection: HTTPConnection, drop: frozenset[str]
) -> list[tuple[bytes, bytes]]:
    """上流へ渡すリクエストヘッダを組む（`x-forwarded-for` とセッションを足す）.

    http と websocket の両方がここを通る（組み立て点は 1 箇所）。

    `X-Forwarded-Proto` / `X-Forwarded-Host` は足さない。uvicorn の
    `ProxyHeadersMiddleware` は `X-Forwarded-Host` を読まず、`trusted_hosts` の既定が
    `127.0.0.1` なので LAN 越しでは無視されるため。
    """
    headers = connection.headers
    forwarded = [
        (key, value)
        for key, value in headers.raw
        if key.decode("latin-1").lower() not in drop | {"x-forwarded-for"}
    ]
    client_host = connection.client.host if connection.client else None
    if client_host:
        existing = headers.get("x-forwarded-for")
        chain = f"{existing}, {client_host}" if existing else client_host
        forwarded.append((b"x-forwarded-for", chain.encode("latin-1")))
    forwarded.extend(_session_headers(connection.cookies))
    return forwarded


def _session_headers(cookies: Mapping[str, str]) -> list[tuple[bytes, bytes]]:
    """ブラウザの cookie を backend が読むセッションヘッダへ翻訳する.

    ブラウザは `img.src`（MJPEG）と WS ハンドシェイクに独自ヘッダを付けられないため、
    セッションを載せられるのは cookie しかない。一方 cookie は backend へ渡さないので、
    翻訳が要る。

    表示名は HTTP/1.1 ヘッダが latin-1 なので `quote` して ASCII にする（生の日本語名は
    uvicorn / httpx が壊す）。JS は `encodeURIComponent` で cookie に書く = 既に
    quote 済みなので、`unquote` を挟んで二重エンコードを避ける（backend の `unquote`
    1 回で元の表示名に戻る）。

    **これは認証ではなく自己申告**で、LAN 上の誰でも他人を騙れる。認証が無い現状より
    悪化しないので受容している。
    """
    headers: list[tuple[bytes, bytes]] = []
    session = cookies.get(SESSION_COOKIE)
    if session:
        # cookie 値は latin-1 のヘッダ由来なので必ず latin-1 へ戻せる
        headers.append((_SESSION_HEADER, session.encode("latin-1")))
    display_name = cookies.get(NAME_COOKIE)
    if display_name and (encoded := _reencoded_name(display_name)) is not None:
        headers.append((_NAME_HEADER, encoded))
    return headers


def _reencoded_name(cookie_value: str) -> bytes | None:
    """表示名 cookie をヘッダ用に組み直す（復元できない値は None = ヘッダを付けない）.

    `unquote` の既定（``errors="replace"``）は壊れた percent-encoding を U+FFFD へ
    「修復」してしまい、それを quote し直すと**正当な encoding として backend へ渡る**。
    backend 側の「復元できない値は既定名へ落とす」防御（`web.api.identity`）が
    到達不能になり、文字化け 1 文字が表示名として全クライアントへ配られる。ここで
    落として backend のフォールバックに委ねる。
    """
    try:
        decoded = unquote(cookie_value, errors="strict")
    except UnicodeDecodeError:
        return None
    return quote(decoded).encode("ascii")


def _forward_websocket_headers(websocket: WebSocket) -> list[tuple[str, str]]:
    return [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in _forward_headers(websocket, _DROP_WEBSOCKET_HEADERS)
    ]


def _has_body(headers: Headers) -> bool:
    return "content-length" in headers or "transfer-encoding" in headers


def _error_response(status_code: int, detail: str) -> JSONResponse:
    # app.js の既存トーストが読む {"detail": …} 形に合わせる
    return JSONResponse(status_code=status_code, content={"detail": detail})
