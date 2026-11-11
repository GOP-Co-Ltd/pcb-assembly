"""FastAPI アプリケーションファクトリ."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.routing import BaseRoute

from pcbasm.machine.devices import Devices
from web.api.backend import Backend, open_backend
from web.api.env import Env
from web.api.errors import install_error_handlers
from web.api.routers import (
    audio,
    backup,
    jobs,
    machine,
    machine_config,
    preview,
    readiness,
    selection,
    session,
    update,
)
from web.api.routers.pasting import board, calibration, nozzle, test_board
from web.api.routers.pnp import servo
from web.selfupdate.runner import UpdateRunner


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    backend: Backend = app.state.backend
    # ワーカースレッド → WS のイベント橋渡し先 loop を登録する
    backend.events.bind_loop(asyncio.get_running_loop())
    # mDNS 広告は running loop を要求するのでここで開始する（create_app は同期）
    if backend.advertiser is not None:
        await backend.advertiser.start()
    # 更新通知のための定期 fetch（`GET /api/update/status` は fetch しないため、
    # これが無いと「更新があります」が誰かの手動確認まで出ない）
    backend.update.start_watching()
    yield
    backend.update.stop_watching()
    if backend.advertiser is not None:
        await backend.advertiser.stop()
    backend.close()


def create_app(
    env: Env | None = None,
    *,
    devices: Devices | None = None,
    paste_test_board_footprint_root: Path | None = None,
    update_runner: UpdateRunner | None = None,
) -> FastAPI:
    """WebUI の FastAPI アプリを構築する.

    Args:
        env: 起動時の環境（None なら環境変数から構築。uvicorn --factory 用）
        devices: 装置（None なら env どおりに組む。テストは fake を注入する）
        paste_test_board_footprint_root: テスト塗布基板で使う
            KiCad footprint root。None なら環境変数または KiCad 9 標準パス
        update_runner: 自己更新のランナー（None なら env から構築）。テストは
            スタブ実行ファイルを差した UpdateSettings 版を注入する

    Returns:
        構成済みの FastAPI アプリ
    """
    if env is None:
        env = Env.from_env()

    app = FastAPI(title="pcb-assembly WebUI", lifespan=_lifespan)
    backend = open_backend(
        env,
        devices=devices,
        paste_test_board_footprint_root=paste_test_board_footprint_root,
        update_runner=update_runner,
    )
    app.state.backend = backend

    # ジョブ成果物の配信（data/webui/<job_id>/...。traversal 防止は StaticFiles）
    artifacts_dir = env.webui_data_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/artifacts", StaticFiles(directory=artifacts_dir), name="artifacts")

    install_error_handlers(app, backend.control)
    capabilities = backend.machine_config.capabilities
    app.router.routes.extend(
        _api_routes(solder="solder" in capabilities, pnp="pnp" in capabilities)
    )
    return app


@cache
def _api_routes(*, solder: bool, pnp: bool) -> tuple[BaseRoute, ...]:
    """機能の組ごとに組み上げた route を、プロセス内で使い回す.

    `include_router` は呼ぶたびに route と pydantic の field を作り直し、create_app の時間の大半を占める。
    組み上げた route は app の状態を持たないので、別の app に同じものを載せてよい。
    ただし app ではなく単独の APIRouter で組むため、route は `app.dependency_overrides` を見ない（使っていない）。
    """
    router = APIRouter()
    for module_router in (
        session.router,
        selection.router,
        machine.router,
        machine.emergency_stop_router,
        audio.router,
        machine_config.router,
        backup.router,
        jobs.router,
        preview.router,
        update.router,
        readiness.router,
    ):
        router.include_router(module_router)
    # 塗布の API は、塗布のジョブと同じく solder 機能の機体にだけ置く
    if solder:
        for module_router in (
            board.router,
            nozzle.router,
            calibration.router,
            test_board.router,
        ):
            router.include_router(module_router)
    # 部品実装の API は、部品実装のジョブと同じく pnp 機能の機体にだけ置く
    if pnp:
        router.include_router(servo.router)
    return tuple(router.routes)
