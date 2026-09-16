"""実サーバー間の通信順序を制御する E2E 用 ASGI ラッパー。"""

import asyncio
from threading import Event

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class DelayedHttp:
    """一致する最初の HTTP 要求または応答を保留し、他の通信はそのまま通す。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        method: str,
        path_suffixes: tuple[str, ...],
        response: bool = False,
    ) -> None:
        self._app = app
        self._method = method
        self._path_suffixes = path_suffixes
        self._delay_response = response
        self._claimed = False
        self._received = Event()
        self._release = Event()

    def delay_response(self) -> None:
        self._delay_response = True

    def wait_received(self) -> bool:
        return self._received.wait(timeout=10)

    def resume(self) -> None:
        self._release.set()

    async def _wait(self) -> None:
        self._received.set()
        await asyncio.to_thread(self._release.wait, 30)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != self._method
            or not scope["path"].endswith(self._path_suffixes)
            or self._claimed
        ):
            await self._app(scope, receive, send)
            return
        self._claimed = True
        if not self._delay_response:
            await self._wait()
            await self._app(scope, receive, send)
            return

        async def delayed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                await self._wait()
            await send(message)

        await self._app(scope, receive, delayed_send)
