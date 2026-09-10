"""FastAPI アプリケーションファクトリ."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from pcbasm.hal import AlsaAudioPlayer, AudioPlayer
from pcbasm.pasting.testboard.config import BoardConfigError
from pcbasm.pasting.testboard.generator import BoardGenerator
from pcbasm.pcb.units import KicadError
from web.api.board_settings import BoardSettingsStore
from web.api.config_store import ConfigStore, UnknownFieldError
from web.api.control import ControlDeniedError, ControlLease, LeaseInfo
from web.api.discovery import (
    ServiceAdvertiser,
    local_ipv4_addresses,
    select_advertise_addresses,
)
from web.api.jobs.catalog import default_catalog
from web.api.jobs.manager import JobManager
from web.api.preview import PreviewService
from web.api.routers import (
    app_state,
    audio,
    control_api,
    files,
    jobs,
    machine_control,
    nozzle_cap,
    paste_dataset,
    paste_test_board,
    pasting,
    pasting_loading,
    preview as preview_router,
    settings_api,
    system,
    update as update_router,
)
from web.api.routers.common import control_payload
from web.api.settings import Settings, resolve_machine_id
from web.api.state import AppState, BusyError
from web.selfupdate.runner import UpdateRunner
from web.selfupdate.settings import UpdateSettings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ワーカースレッド → WS のイベント橋渡し先 loop を登録する
    app.state.jobs.bind_loop(asyncio.get_running_loop())
    # mDNS 広告は running loop を要求するのでここで開始する（create_app は同期）
    advertiser: ServiceAdvertiser | None = app.state.advertiser
    if advertiser is not None:
        await advertiser.start()
    yield
    if advertiser is not None:
        await advertiser.stop()
    # シャットダウン後始末（preview 通知 → ジョブ join → 通知音終了 → FrameHub 停止）
    # 通知音の close はジョブ join 後（実行中ジョブの完了音を捨てない）
    app.state.preview.request_shutdown()
    app.state.jobs.shutdown()
    app.state.audio_player.close()
    app.state.appstate.close()


def _build_advertiser(settings: Settings, state: AppState) -> ServiceAdvertiser:
    """設定と machine.toml の現在値から広告を組む（ソケットは開かない）.

    フィルタ（`select_advertise_addresses`）を掛けるのは実 IF から列挙したときだけ。
    ``advertise_addresses`` の注入はそのまま使う（loopback を落とすと、この注入口の
    目的である「テストをループバックに閉じる」が成立しない）。
    """
    addresses = settings.advertise_addresses
    if addresses is None:
        addresses = select_advertise_addresses(local_ipv4_addresses())
    return ServiceAdvertiser(
        machine_id=resolve_machine_id(settings),
        port=settings.port,
        name=state.machine_name(),
        machine_type=state.machine_type(),
        addresses=addresses,
        service_type=settings.discovery_service_type,
        interfaces=settings.discovery_interfaces,
    )


def _build_update_runner(settings: Settings) -> UpdateRunner:
    """設定から自己更新のランナーを組む（リポジトリは常にこのソースツリー）.

    ブランチ・remote・`uv` の引数はサーバ側の固定値。リクエストからは触れない。
    """
    return UpdateRunner(
        UpdateSettings(
            state_dir=settings.update_dir,
            uv_sync_args=settings.update_uv_sync_args,
            enabled=settings.update_enabled,
        )
    )


def _control_change_notifier(app: FastAPI) -> Callable[[], None]:
    """保持者が変わったことを全 WS 購読者へ配る `on_change` を作る.

    リース自身を参照するので、生成時ではなく呼び出し時に `app.state` から読む。
    """

    def notify() -> None:
        jobs: JobManager = app.state.jobs
        lease: ControlLease = app.state.control
        jobs.publish(control_api.control_changed_event(lease.snapshot()))

    return notify


def create_app(
    settings: Settings | None = None,
    *,
    audio_player: AudioPlayer | None = None,
    clock: Callable[[], float] | None = None,
    paste_test_board_footprint_root: Path | None = None,
    update_runner: UpdateRunner | None = None,
) -> FastAPI:
    """WebUI の FastAPI アプリを構築する.

    Args:
        settings: WebUI 設定（None なら環境変数から構築。uvicorn --factory 用）
        audio_player: 通知音プレイヤー（None なら ALSA 実装を構築）
        clock: 操作権リースの時計（None なら `time.monotonic`）。失効までの秒数は
            分単位なので、実時間で待つと検証できない。「ジョブ実行中は無操作でも
            失効しない」という `busy` の配線を確かめるための注入口
        paste_test_board_footprint_root: テスト塗布基板で使う
            KiCad footprint root。Noneなら環境変数またはKiCad 9標準パス
        update_runner: 自己更新のランナー（None なら settings から構築）。テストは
            スタブ実行ファイルを差した UpdateSettings 版を注入する

    Returns:
        構成済みの FastAPI アプリ
    """
    if settings is None:
        settings = Settings.from_env()

    store = ConfigStore(settings.config_dir)
    if not store.machine_toml_path().is_file():
        raise RuntimeError(
            f"machine.toml がありません: {store.machine_toml_path()} "
            f"（./scripts/setup-machine-config.sh を実行してください）"
        )
    state = AppState(settings, store)
    preview = PreviewService(state)
    catalog = default_catalog()
    if audio_player is None:
        audio_player = AlsaAudioPlayer()

    app = FastAPI(title="pcb-assembly WebUI", lifespan=_lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.appstate = state
    app.state.preview = preview
    app.state.catalog = catalog
    app.state.audio_player = audio_player
    app.state.paste_test_board_generator = BoardGenerator(
        paste_test_board_footprint_root
    )
    board_store = BoardSettingsStore(
        settings.webui_data_dir, legacy_root=settings.data_dir / "board_settings"
    )
    app.state.board_store = board_store
    app.state.jobs = JobManager(
        state, preview, catalog, settings, board_store, audio_player=audio_player
    )
    # 広告の開始は lifespan（AsyncZeroconf が running loop を要求する）
    app.state.advertiser = (
        _build_advertiser(settings, state) if settings.discovery_enabled else None
    )
    app.state.update = (
        update_runner if update_runner is not None else _build_update_runner(settings)
    )
    # 操作権リース。無操作失効の判定は装置排他ロック（= ジョブ実行中）で止める
    app.state.control = ControlLease(
        clock=clock if clock is not None else time.monotonic,
        busy=lambda: state.busy_owner is not None,
        on_change=_control_change_notifier(app),
    )

    # ジョブ成果物の配信（data/webui/<job_id>/...。traversal 防止は StaticFiles）
    artifacts_dir = settings.webui_data_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/artifacts", StaticFiles(directory=artifacts_dir), name="artifacts")

    @app.exception_handler(BusyError)
    async def busy_error_handler(request: Request, exc: BusyError) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "owner": exc.owner}
        )

    @app.exception_handler(ControlDeniedError)
    async def control_denied_handler(
        request: Request, exc: ControlDeniedError
    ) -> JSONResponse:
        """操作権の拒否を 423 Locked + 保持者情報にする.

        `ControlDep` が**ハンドラ本体に入る前**に投げるため、`klipper_errors_to_502()`
        の RuntimeError → 502 変換に巻き込まれない。
        """
        holder: LeaseInfo = request.app.state.control.snapshot()
        return JSONResponse(
            status_code=423,
            content={
                "detail": str(exc),
                "holder": (
                    control_payload(holder).model_dump(mode="json")
                    if holder.held
                    else None
                ),
            },
        )

    @app.exception_handler(UnknownFieldError)
    async def unknown_field_handler(
        request: Request, exc: UnknownFieldError
    ) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(BoardConfigError)
    async def paste_test_board_config_error_handler(
        request: Request, exc: BoardConfigError
    ) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(KicadError)
    async def kicad_error_handler(request: Request, exc: KicadError) -> JSONResponse:
        # KiCad footprint library / 座標範囲のエラー（テスト塗布基板生成）
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    app.include_router(app_state.router)
    app.include_router(control_api.router)
    app.include_router(files.router)
    app.include_router(settings_api.router)
    app.include_router(audio.router)
    app.include_router(machine_control.router)
    app.include_router(system.router)
    app.include_router(update_router.router)
    app.include_router(preview_router.router)
    app.include_router(jobs.router)
    app.include_router(pasting.router)
    app.include_router(paste_dataset.router)
    app.include_router(paste_test_board.router)
    app.include_router(pasting_loading.router)
    app.include_router(nozzle_cap.router)
    return app
