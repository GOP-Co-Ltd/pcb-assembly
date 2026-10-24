"""FastAPI アプリケーションファクトリ."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

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
    selection,
    session,
    update,
)
from web.api.routers.pasting import board, calibration, nozzle, test_board
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
    for router in (
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
        board.router,
        nozzle.router,
        calibration.router,
        test_board.router,
    ):
        app.include_router(router)
    return app
