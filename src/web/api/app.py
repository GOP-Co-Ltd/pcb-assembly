"""FastAPI アプリケーションファクトリ."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import override
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from web.api.board_settings import BoardSettingsStore
from web.api.config_store import ConfigStore, UnknownFieldError
from web.api.jobs.catalog import default_catalog
from web.api.jobs.manager import JobManager
from web.api.preview import PreviewService
from web.api.routers import (
    app_state,
    files,
    jobs,
    machine_control,
    nozzle_cap,
    pages,
    pasting,
    pasting_loading,
    preview as preview_router,
    settings_api,
    system,
)
from web.api.settings import Settings
from web.api.state import AppState, BusyError

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
    normalized = path.lstrip("/")
    asset_path = _STATIC_DIR / normalized
    version = asset_path.stat().st_mtime_ns if asset_path.is_file() else 0
    return f"/static/{quote(normalized, safe='/')}?v={version}"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ワーカースレッド → WS のイベント橋渡し先 loop を登録する
    app.state.jobs.bind_loop(asyncio.get_running_loop())
    yield
    # シャットダウン後始末（preview 終了通知 → ジョブ abort + join → FrameHub 停止）
    app.state.preview.request_shutdown()
    app.state.jobs.shutdown()
    app.state.appstate.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """WebUI の FastAPI アプリを構築する.

    Args:
        settings: WebUI 設定（None なら環境変数から構築。uvicorn --factory 用）

    Returns:
        構成済みの FastAPI アプリ
    """
    if settings is None:
        settings = Settings.from_env()

    store = ConfigStore(settings.config_dir)
    if not store.machine_toml_path().is_file():
        raise RuntimeError(
            f"machine.toml がありません: {store.machine_toml_path()} "
            f"（./setup-machine-config.sh を実行してください）"
        )
    state = AppState(settings, store)
    preview = PreviewService(state)
    catalog = default_catalog()

    app = FastAPI(title="pcb-assembly WebUI", lifespan=_lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.appstate = state
    app.state.preview = preview
    app.state.catalog = catalog
    board_store = BoardSettingsStore(
        settings.webui_data_dir, legacy_root=settings.data_dir / "board_settings"
    )
    app.state.board_store = board_store
    app.state.jobs = JobManager(state, preview, catalog, settings, board_store)
    app.state.templates = Jinja2Templates(directory=_PACKAGE_DIR / "templates")
    app.state.templates.env.globals["static_asset"] = _static_asset_url
    app.mount("/static", _NoCacheStaticFiles(directory=_STATIC_DIR), name="static")

    # ジョブ成果物の配信（data/webui/<job_id>/...。traversal 防止は StaticFiles）
    artifacts_dir = settings.webui_data_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/artifacts", StaticFiles(directory=artifacts_dir), name="artifacts")

    @app.exception_handler(BusyError)
    async def busy_error_handler(request: Request, exc: BusyError) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "owner": exc.owner}
        )

    @app.exception_handler(UnknownFieldError)
    async def unknown_field_handler(
        request: Request, exc: UnknownFieldError
    ) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.include_router(app_state.router)
    app.include_router(files.router)
    app.include_router(settings_api.router)
    app.include_router(machine_control.router)
    app.include_router(system.router)
    app.include_router(preview_router.router)
    app.include_router(jobs.router)
    app.include_router(pasting.router)
    app.include_router(pasting_loading.router)
    app.include_router(nozzle_cap.router)
    # /{tab} のキャッチオールを持つため最後に登録する
    app.include_router(pages.router)
    return app
