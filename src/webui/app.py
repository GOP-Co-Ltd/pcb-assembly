"""FastAPI アプリケーションファクトリと依存取得ヘルパ."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from webui.config_store import ConfigStore, UnknownFieldError
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState, BusyError

_PACKAGE_DIR = Path(__file__).parent


def get_state(request: Request) -> AppState:
    return request.app.state.appstate


def get_store(request: Request) -> ConfigStore:
    return request.app.state.store


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def get_preview(request: Request) -> PreviewService:
    return request.app.state.preview


StateDep = Annotated[AppState, Depends(get_state)]
StoreDep = Annotated[ConfigStore, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
PreviewDep = Annotated[PreviewService, Depends(get_preview)]


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # シャットダウン後始末（FrameHub 停止 + カメラ参照破棄）
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

    store = ConfigStore(settings.configs_root)
    state = AppState(settings, store)

    app = FastAPI(title="pcb-assembly WebUI", lifespan=_lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.appstate = state
    app.state.preview = PreviewService(state)
    app.state.templates = Jinja2Templates(directory=_PACKAGE_DIR / "templates")
    app.mount("/static", StaticFiles(directory=_PACKAGE_DIR / "static"), name="static")

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

    # 循環 import を避けるため、ルーターはここで import する
    from webui.routers import (
        files,
        machine,
        machine_control,
        pages,
        preview,
        settings_api,
        system,
    )

    app.include_router(machine.router)
    app.include_router(files.router)
    app.include_router(settings_api.router)
    app.include_router(machine_control.router)
    app.include_router(system.router)
    app.include_router(preview.router)
    # /{tab} のキャッチオールを持つため最後に登録する
    app.include_router(pages.router)
    return app
