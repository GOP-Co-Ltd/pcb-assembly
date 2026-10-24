"""FastAPI の依存取得ヘルパ（app.state からの解決と型エイリアス）."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from pcbasm.hal import AudioPlayer
from pcbasm.pasting.overrides import BoardOverrides
from pcbasm.pasting.testboard.generator import (
    BoardGenerator,
)
from web.api.control import ClientIdentity, ControlLease, LeaseInfo
from web.api.identity import get_identity
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.manager import JobManager
from web.api.machine_config import MachineConfigFile
from web.api.preview import PreviewService
from web.api.settings import Settings
from web.api.state import AppState
from web.selfupdate.runner import UpdateRunner


def get_state(request: Request) -> AppState:
    return request.app.state.appstate


def get_machine_config(request: Request) -> MachineConfigFile:
    return request.app.state.machine_config


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


def get_overrides(request: Request) -> BoardOverrides:
    return request.app.state.overrides


def get_paste_test_board_generator(
    request: Request,
) -> BoardGenerator:
    return request.app.state.paste_test_board_generator


def get_update(request: Request) -> UpdateRunner:
    """自己更新のランナー（ホストに 1 つ。装置ドメインではなくデプロイ運用）."""
    return request.app.state.update


def get_lease(request: Request) -> ControlLease:
    return request.app.state.control


def require_control(request: Request) -> LeaseInfo:
    """操作権を検証する（空きなら取得する。変更系エンドポイント用）.

    **必ず `Depends`（= `ControlDep`）として使う。**

    Raises:
        ControlDeniedError: 他クライアントが操作権を保持している場合（``web.api.errors``
            が 423 Locked へ変換する）
    """
    return request.app.state.control.claim(get_identity(request))


StateDep = Annotated[AppState, Depends(get_state)]
MachineConfigDep = Annotated[MachineConfigFile, Depends(get_machine_config)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
PreviewDep = Annotated[PreviewService, Depends(get_preview)]
JobsDep = Annotated[JobManager, Depends(get_jobs)]
AudioPlayerDep = Annotated[AudioPlayer, Depends(get_audio_player)]
CatalogDep = Annotated[JobCatalog, Depends(get_catalog)]
OverridesDep = Annotated[BoardOverrides, Depends(get_overrides)]
BoardGeneratorDep = Annotated[
    BoardGenerator,
    Depends(get_paste_test_board_generator),
]
IdentityDep = Annotated[ClientIdentity, Depends(get_identity)]
LeaseDep = Annotated[ControlLease, Depends(get_lease)]
UpdateDep = Annotated[UpdateRunner, Depends(get_update)]
# 変更系エンドポイントの認可。ハンドラ本体に入る前に 423 で断る
ControlDep = Annotated[LeaseInfo, Depends(require_control)]
