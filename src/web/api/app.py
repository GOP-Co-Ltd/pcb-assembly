"""FastAPI アプリケーションファクトリ."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from pcbasm.hal import AlsaAudioPlayer, AudioPlayer
from pcbasm.machine.devices import Devices
from pcbasm.pasting.overrides import BoardOverrides
from pcbasm.pasting.test_board.generator import BoardGenerator
from web.api.control import ControlLease
from web.api.discovery import (
    ServiceAdvertiser,
    local_ipv4_addresses,
    select_advertise_addresses,
)
from web.api.env import Env, resolve_machine_id
from web.api.errors import install_error_handlers
from web.api.jobs.catalog import default_catalog
from web.api.jobs.manager import JobManager
from web.api.machine_config import MachineConfigFile
from web.api.preview import PreviewService
from web.api.routers import (
    app_state,
    audio,
    backup,
    control_api,
    files,
    jobs,
    machine_config as machine_config_router,
    machine_control,
    nozzle_cap,
    paste_dataset,
    paste_test_board,
    paste_volume,
    pasting,
    pasting_loading,
    preview as preview_router,
    system,
    update as update_router,
)
from web.api.state import AppState
from web.selfupdate.runner import UpdateRunner
from web.selfupdate.settings import UpdateSettings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ワーカースレッド → WS のイベント橋渡し先 loop を登録する
    app.state.jobs.bind_loop(asyncio.get_running_loop())
    # mDNS 広告は running loop を要求するのでここで開始する（create_app は同期）
    advertiser: ServiceAdvertiser | None = app.state.advertiser
    if advertiser is not None:
        await advertiser.start()
    # 更新通知のための定期 fetch（`GET /api/update/status` は fetch しないため、
    # これが無いと「更新があります」が誰かの手動確認まで出ない）
    app.state.update.start_watching()
    yield
    app.state.update.stop_watching()
    if advertiser is not None:
        await advertiser.stop()
    # シャットダウン後始末（preview 通知 → ジョブ join → 通知音終了 → FrameHub 停止）
    # 通知音の close はジョブ join 後（実行中ジョブの完了音を捨てない）
    app.state.preview.request_shutdown()
    app.state.jobs.shutdown()
    app.state.audio_player.close()
    app.state.appstate.close()


def _build_advertiser(settings: Env) -> ServiceAdvertiser:
    """設定から広告を組む（ソケットは開かない）.

    設定エラー（degraded）でも広告する。機体一覧に載らないと WebUI から直せない。

    フィルタ（`select_advertise_addresses`）を掛けるのは実 IF から列挙したときだけ。
    ``advertise_addresses`` の注入はそのまま使う（loopback を除外すると、この注入口の
    目的である「テストをループバックに閉じる」が成立しない）。
    """
    addresses = settings.advertise_addresses
    if addresses is None:
        addresses = select_advertise_addresses(local_ipv4_addresses())
    return ServiceAdvertiser(
        machine_id=resolve_machine_id(settings),
        port=settings.port,
        addresses=addresses,
        service_type=settings.discovery_service_type,
        interfaces=settings.discovery_interfaces,
    )


def _build_update_runner(settings: Env) -> UpdateRunner:
    """設定から自己更新のランナーを組む（リポジトリは常にこのソースツリー）.

    ブランチ・remote・`uv` の引数はサーバ側の固定値。リクエストからは変更できない。
    """
    return UpdateRunner(
        UpdateSettings(
            state_dir=settings.update_state_dir,
            uv_sync_args=settings.update_uv_sync_args,
            enabled=settings.update_enabled,
        )
    )


def _state_change_notifier(app: FastAPI) -> Callable[[], None]:
    """machine.toml を書いたことを全 WS 購読者へ配る `on_change` を作る.

    ジョブマネージャは machine.toml の読み書きより後に組むので、呼び出し時に `app.state` から読む。
    """

    def notify() -> None:
        jobs: JobManager = app.state.jobs
        jobs.publish_state_changed()

    return notify


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
    settings: Env | None = None,
    *,
    devices: Devices | None = None,
    audio_player: AudioPlayer | None = None,
    paste_test_board_footprint_root: Path | None = None,
    update_runner: UpdateRunner | None = None,
) -> FastAPI:
    """WebUI の FastAPI アプリを構築する.

    Args:
        settings: WebUI 設定（None なら環境変数から構築。uvicorn --factory 用）
        devices: 装置（None なら settings どおりに組む。テストは fake を注入する）
        audio_player: 通知音プレイヤー（None なら ALSA 実装を構築）
        paste_test_board_footprint_root: テスト塗布基板で使う
            KiCad footprint root。None なら環境変数または KiCad 9 標準パス
        update_runner: 自己更新のランナー（None なら settings から構築）。テストは
            スタブ実行ファイルを差した UpdateSettings 版を注入する

    Returns:
        構成済みの FastAPI アプリ
    """
    if settings is None:
        settings = Env.from_env()

    app = FastAPI(title="pcb-assembly WebUI", lifespan=_lifespan)
    machine_config = MachineConfigFile(
        settings.config_dir, on_change=_state_change_notifier(app)
    )
    if not machine_config.path.is_file():
        raise RuntimeError(
            f"machine.toml がありません: {machine_config.path} "
            f"（./scripts/setup-machine-config.sh を実行してください）"
        )
    state = AppState(settings, machine_config, devices)
    state.migrate_config()
    # 設定エラーでもプロセスは落とさず degraded で上げる（落とすと WebUI から直せない）
    _, config_error = state.machine_or_error()
    if config_error is not None:
        logger.warning("設定エラーのため degraded で起動します: %s", config_error)
    preview = PreviewService(state)
    catalog = default_catalog()
    if audio_player is None:
        audio_player = AlsaAudioPlayer()

    app.state.settings = settings
    app.state.machine_config = machine_config
    app.state.appstate = state
    app.state.preview = preview
    app.state.catalog = catalog
    app.state.audio_player = audio_player
    app.state.paste_test_board_generator = BoardGenerator(
        paste_test_board_footprint_root
    )
    overrides = BoardOverrides(settings.webui_data_dir)
    app.state.overrides = overrides
    app.state.jobs = JobManager(
        state, preview, catalog, settings, overrides, audio_player=audio_player
    )
    # 広告の開始は lifespan（AsyncZeroconf が running loop を要求する）
    app.state.advertiser = (
        _build_advertiser(settings) if settings.discovery_enabled else None
    )
    app.state.update = (
        update_runner if update_runner is not None else _build_update_runner(settings)
    )
    # 操作権リース（自動解放しない。プロセス再起動で空きに戻る）
    app.state.control = ControlLease(on_change=_control_change_notifier(app))

    # ジョブ成果物の配信（data/webui/<job_id>/...。traversal 防止は StaticFiles）
    artifacts_dir = settings.webui_data_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/artifacts", StaticFiles(directory=artifacts_dir), name="artifacts")

    install_error_handlers(app)
    app.include_router(app_state.router)
    app.include_router(control_api.router)
    app.include_router(files.router)
    app.include_router(machine_config_router.router)
    app.include_router(audio.router)
    app.include_router(machine_control.router)
    app.include_router(system.router)
    app.include_router(update_router.router)
    app.include_router(backup.router)
    app.include_router(preview_router.router)
    app.include_router(jobs.router)
    app.include_router(pasting.router)
    app.include_router(paste_dataset.router)
    app.include_router(paste_volume.router)
    app.include_router(paste_test_board.router)
    app.include_router(pasting_loading.router)
    app.include_router(nozzle_cap.router)
    return app
