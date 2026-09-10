"""UI frontend の FastAPI アプリケーションファクトリ."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import override
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from web.selfupdate.runner import UpdateRunner
from web.selfupdate.settings import UpdateSettings
from web.ui import machines_api, pages, update_api
from web.ui.discovery import MachineDiscovery
from web.ui.machine_client import BackendGateway, BackendUnavailable
from web.ui.machines import (
    MachineEndpoint,
    MachineRegistry,
    UnknownMachine,
    load_machines_file,
)
from web.ui.proxy import ProxyApp
from web.ui.settings import Settings

_PACKAGE_DIR = Path(__file__).parent
_STATIC_DIR = _PACKAGE_DIR / "static"
_STATIC_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"


class _NoCacheStaticFiles(StaticFiles):
    """静的アセットに再検証ヘッダを付ける StaticFiles."""

    @override
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = _STATIC_CACHE_CONTROL
        return response


def _static_asset_url(path: str) -> str:
    """静的アセットの URL に mtime のキャッシュバスターを付ける.

    ``/static`` は machine prefix を付けない（ブラウザキャッシュを全マシンで 1 本
    共有する。アセットは frontend の所有物でマシンごとに変わらない）。
    """
    normalized = path.lstrip("/")
    asset_path = _STATIC_DIR / normalized
    version = asset_path.stat().st_mtime_ns if asset_path.is_file() else 0
    return f"/static/{quote(normalized, safe='/')}?v={version}"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # mDNS 探索は running loop を要求するのでここで開始する（create_app は同期）
    discovery: MachineDiscovery | None = app.state.discovery
    if discovery is not None:
        await discovery.start()
    yield
    if discovery is not None:
        await discovery.stop()
    # backend への keep-alive 接続を閉じる
    await app.state.gateway.aclose()


def create_app(
    settings: Settings | None = None,
    *,
    transport_factory: (
        Callable[[MachineEndpoint], httpx.AsyncBaseTransport] | None
    ) = None,
    update_runner: UpdateRunner | None = None,
) -> FastAPI:
    """UI frontend の FastAPI アプリを構築する.

    machine.toml もカメラも要求しない（装置の値はすべて backend から取る）ので、
    機体でないホストでも起動できる。

    Args:
        settings: frontend 設定（None なら環境変数から構築。uvicorn --factory 用）
        transport_factory: backend への transport の差し替え（テストで in-process の
            backend app を挿す。None なら実 TCP）
        update_runner: frontend 自身の更新ランナー（None なら settings から構築）。
            テストはスタブ実行ファイルを差した UpdateSettings 版を注入する

    Returns:
        構成済みの FastAPI アプリ
    """
    if settings is None:
        settings = Settings.from_env()

    registry = MachineRegistry(
        (
            *settings.machines,
            *load_machines_file(
                settings.machines_file, default_port=settings.default_backend_port
            ),
        )
    )
    gateway = BackendGateway(
        connect_timeout=settings.backend_connect_timeout,
        # SSR の既定。プロキシはリクエスト毎に上書きする（MJPEG は無制限、
        # その他は proxy_read_timeout）
        read_timeout=settings.ssr_timeout,
        transport_factory=transport_factory,
    )

    app = FastAPI(title="pcb-assembly UI", lifespan=_lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.gateway = gateway
    # 探索の開始は lifespan（AsyncZeroconf が running loop を要求する）
    app.state.update = (
        update_runner
        if update_runner is not None
        else UpdateRunner(
            UpdateSettings(
                state_dir=settings.update_state_dir,
                uv_sync_args=settings.update_uv_sync_args,
                enabled=settings.update_enabled,
            )
        )
    )
    app.state.discovery = (
        MachineDiscovery(
            on_change=registry.set_discovered,
            service_type=settings.discovery_service_type,
            interfaces=settings.discovery_interfaces,
        )
        if settings.discovery_enabled
        else None
    )
    templates = Jinja2Templates(directory=_PACKAGE_DIR / "templates")
    templates.env.globals["static_asset"] = _static_asset_url
    app.state.templates = templates

    @app.exception_handler(BackendUnavailable)
    async def backend_unavailable_handler(
        request: Request, exc: BackendUnavailable
    ) -> HTMLResponse:
        # ドロップダウンは frontend の登録一覧から描くので backend 不要 = 必ず描ける
        return pages.render_message(
            request,
            title="マシンに接続できません",
            detail=f"{exc.endpoint.label} が応答しません: {exc}",
            status_code=503,
            headers={"Retry-After": "5"},
            machine_id=exc.endpoint.machine_id,
        )

    @app.exception_handler(UnknownMachine)
    async def unknown_machine_handler(
        request: Request, exc: UnknownMachine
    ) -> HTMLResponse:
        # machine_id は渡さない（未知のマシンを base にするとタブが全部 404 になる）
        return pages.render_message(
            request,
            title="未知のマシンです",
            detail=str(exc),
            status_code=404,
        )

    # プロキシは pages ルータより先に登録する。逆にすると /m/x/api/state が
    # /m/{machine_id}/{tab}/{feature} に食われて 404 になる
    app.mount(
        "/m/{machine_id}/api",
        ProxyApp(
            registry,
            gateway,
            target_prefix="/api",
            read_timeout=settings.proxy_read_timeout,
        ),
    )
    app.mount(
        "/m/{machine_id}/artifacts",
        ProxyApp(
            registry,
            gateway,
            target_prefix="/artifacts",
            read_timeout=settings.proxy_read_timeout,
        ),
    )
    # prefix なし（全マシンでブラウザキャッシュを共有する）
    app.mount("/static", _NoCacheStaticFiles(directory=_STATIC_DIR), name="static")
    # pages ルータより先に登録する（後だと /{tab} のキャッチオールに食われて
    # ドロップダウン更新が HTML を受け取る）
    app.include_router(machines_api.router)
    # 同上。/update が /{tab} に食われると更新ページがマシンピッカーになる
    app.include_router(update_api.router)
    # /{tab} のキャッチオールを持つため最後に登録する
    app.include_router(pages.router)
    return app
