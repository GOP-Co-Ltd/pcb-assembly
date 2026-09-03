"""FastAPI の依存取得ヘルパ（app.state からの解決と型エイリアス）."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from pcbasm.hal import AudioPlayer
from pcbasm.pasting.paste_flow_calibration_board.generator import (
    PasteFlowCalibrationBoardGenerator,
)
from web.api.board_settings import BoardSettingsStore
from web.api.config_store import ConfigStore
from web.api.control import ClientIdentity, ControlLease, LeaseInfo
from web.api.discovery import ServiceAdvertiser
from web.api.identity import get_identity
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.manager import JobManager
from web.api.preview import PreviewService
from web.api.settings import Settings
from web.api.state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.appstate


def get_store(request: Request) -> ConfigStore:
    return request.app.state.store


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_preview(request: Request) -> PreviewService:
    return request.app.state.preview


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def get_audio_player(request: Request) -> AudioPlayer:
    return request.app.state.audio_player


def get_catalog(request: Request) -> JobCatalog:
    return request.app.state.catalog


def get_board_store(request: Request) -> BoardSettingsStore:
    return request.app.state.board_store


def get_paste_flow_calibration_board_generator(
    request: Request,
) -> PasteFlowCalibrationBoardGenerator:
    return request.app.state.paste_flow_calibration_board_generator


def get_advertiser(request: Request) -> ServiceAdvertiser | None:
    """広告オブジェクト（`discovery_enabled` が False のアプリでは None）."""
    return request.app.state.advertiser


def get_lease(request: Request) -> ControlLease:
    return request.app.state.control


def require_control(request: Request) -> LeaseInfo:
    """操作権を検証し、保持者の無操作タイマーを更新する（変更系エンドポイント用）.

    **必ず `Depends`（= `ControlDep`）として使う。** ハンドラ本体で `claim` を呼ぶと、
    Klipper 通信エラーを 502 へ変換する `klipper_errors_to_502()` が
    `ControlDeniedError`（`RuntimeError` 派生）を巻き込み、操作権の拒否が
    「Klipper 通信エラー 502」に化ける。

    Raises:
        ControlDeniedError: 他クライアントが操作権を保持している場合（app.py の
            例外ハンドラが 423 Locked へ変換する）
    """
    return request.app.state.control.claim(get_identity(request))


StateDep = Annotated[AppState, Depends(get_state)]
StoreDep = Annotated[ConfigStore, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
PreviewDep = Annotated[PreviewService, Depends(get_preview)]
JobsDep = Annotated[JobManager, Depends(get_jobs)]
AudioPlayerDep = Annotated[AudioPlayer, Depends(get_audio_player)]
CatalogDep = Annotated[JobCatalog, Depends(get_catalog)]
BoardStoreDep = Annotated[BoardSettingsStore, Depends(get_board_store)]
PasteFlowCalibrationBoardGeneratorDep = Annotated[
    PasteFlowCalibrationBoardGenerator,
    Depends(get_paste_flow_calibration_board_generator),
]
AdvertiserDep = Annotated[ServiceAdvertiser | None, Depends(get_advertiser)]
IdentityDep = Annotated[ClientIdentity, Depends(get_identity)]
LeaseDep = Annotated[ControlLease, Depends(get_lease)]
# 変更系エンドポイントの認可。ハンドラ本体に入る前に 423 で断る
ControlDep = Annotated[LeaseInfo, Depends(require_control)]
