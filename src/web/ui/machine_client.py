"""Backend WebAPI (`web.api`) への HTTP クライアント.

SSR ページのハンドラが backend のクライアントになる。取得した値は
`web.api.models` の pydantic モデル（pydantic のみを import する contract モジュール）
で検証してからテンプレートへ渡す。

**backend のレスポンスはキャッシュしない。** frontend は backend の WS を自前で
購読しないため、他の操作者による変更を無効化する条件を正しく書けない。古い値の
フォームを見て書き込み操作をされるほうが、毎ページで取り直すコストより悪い。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from pydantic import BaseModel

from web.api.models import (
    JobCatalogResponse,
    MachineInfo,
    MachineSettingsResponse,
    StateResponse,
    UpdateStatusResponse,
)
from web.ui.machines import MachineEndpoint


class BackendUnavailable(RuntimeError):
    """Backend への到達失敗、または応答が契約を満たさない（→ 503 ページ）.

    ページ描画に必要な取得のうち **1 本でも失敗したらこれを投げる**（部分的に
    欠けた値でフォームを描くと、それを見て書き込み操作をされる）。
    """

    def __init__(self, endpoint: MachineEndpoint, cause: Exception) -> None:
        """例外を初期化する.

        Args:
            endpoint: 到達に失敗した backend
            cause: 原因となった例外（メッセージに畳み込む。連鎖は ``raise … from``）
        """
        super().__init__(
            f"backend に到達できません: {endpoint.base_url} "
            f"({type(cause).__name__}: {cause})"
        )
        # 503 ページに「どのマシンが応答しないか」を出すため public
        self.endpoint = endpoint


class BackendGateway:
    """machine_id ごとに `httpx.AsyncClient` を保持する（接続を使い回す）.

    SSR は 1 ページで複数の backend 取得を並列に行うため、リクエストごとに
    クライアントを作ると TCP ハンドシェイクが毎回走る。

    `read_timeout` は既定値であり、プロキシは用途ごとに上書きする
    （MJPEG は無制限、その他は ``proxy_read_timeout``）。SSR はこの既定値
    （``ssr_timeout``）で待つ。

    クライアントは ``machine_id`` ごとに初回の ``base_url`` で作り、以後差し替えない。
    mDNS だけで見つかったマシンの host/port が変わっても、SSR（相対パスで取る `MachineClient`）は再起動まで古い宛先へ繋ぐ。
    `ProxyApp` は絶対 URL を渡すので、中継は常に最新の host/port へ行く。
    """

    def __init__(
        self,
        *,
        connect_timeout: float,
        read_timeout: float,
        transport_factory: (
            Callable[[MachineEndpoint], httpx.AsyncBaseTransport] | None
        ) = None,
    ) -> None:
        """Gateway を初期化する.

        Args:
            connect_timeout: TCP 接続確立の待ち時間 [s]
            read_timeout: 応答の待ち時間 [s]（呼び出し側が上書き可能な既定値）
            transport_factory: transport の差し替え（テストで in-process の
                backend app を挿す。None なら httpx の既定 = 実 TCP）
        """
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._transport_factory = transport_factory
        self._clients: dict[str, httpx.AsyncClient] = {}

    def client_for(self, endpoint: MachineEndpoint) -> httpx.AsyncClient:
        """エンドポイント宛のクライアント（初回のみ生成してキャッシュ）."""
        client = self._clients.get(endpoint.machine_id)
        if client is None:
            client = httpx.AsyncClient(
                base_url=endpoint.base_url,
                timeout=httpx.Timeout(
                    self._read_timeout, connect=self._connect_timeout
                ),
                transport=(
                    None
                    if self._transport_factory is None
                    else self._transport_factory(endpoint)
                ),
            )
            self._clients[endpoint.machine_id] = client
        return client

    async def aclose(self) -> None:
        """保持している全クライアントを閉じる（lifespan の終了時に呼ぶ）."""
        clients = tuple(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.aclose()


class MachineClient:
    """1 台の backend から SSR ページに必要な値を取得する."""

    def __init__(self, endpoint: MachineEndpoint, gateway: BackendGateway) -> None:
        """クライアントを初期化する.

        Args:
            endpoint: 対象の backend
            gateway: クライアントの供給元（接続を使い回す）
        """
        self._endpoint = endpoint
        self._gateway = gateway

    async def machine_info(self) -> MachineInfo:
        """`GET /api/machine-info`（到達性プローブ兼用）."""
        return await self._fetch(MachineInfo, "/api/machine-info")

    async def state(self) -> StateResponse:
        """`GET /api/state`."""
        return await self._fetch(StateResponse, "/api/state")

    async def machine_settings(self) -> MachineSettingsResponse:
        """`GET /api/settings/machine`."""
        return await self._fetch(MachineSettingsResponse, "/api/settings/machine")

    async def update_status(self) -> UpdateStatusResponse:
        """`GET /api/update/status`（fetch を伴わないので毎分読んでよい）."""
        return await self._fetch(UpdateStatusResponse, "/api/update/status")

    async def jobs(self) -> JobCatalogResponse:
        """`GET /api/jobs`（hidden を含む全件）."""
        return await self._fetch(JobCatalogResponse, "/api/jobs")

    async def _fetch[ModelT: BaseModel](self, model: type[ModelT], path: str) -> ModelT:
        """Backend から 1 本取得して検証する.

        Raises:
            BackendUnavailable: 到達失敗・4xx/5xx・JSON やスキーマの不一致
        """
        client = self._gateway.client_for(self._endpoint)
        try:
            response = await client.get(path)
            response.raise_for_status()
            return model.model_validate_json(response.content)
        # ValueError は json デコード失敗と pydantic の ValidationError を含む。
        # 契約を満たさない応答でページを描くのは到達できないのと同じ扱いにする
        except (httpx.HTTPError, ValueError) as exc:
            raise BackendUnavailable(self._endpoint, exc) from exc
