"""FastAPI の依存取得ヘルパ（app.state からの解決と型エイリアス）."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates

from pcbasm.hal import AudioPlayer
from webui.board_settings import BoardSettingsStore
from webui.config_store import ConfigStore
from webui.jobs.catalog import JobCatalog
from webui.jobs.manager import JobManager
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState


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


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def get_audio_player(request: Request) -> AudioPlayer:
    return request.app.state.audio_player


def get_catalog(request: Request) -> JobCatalog:
    return request.app.state.catalog


def get_board_store(request: Request) -> BoardSettingsStore:
    return request.app.state.board_store


StateDep = Annotated[AppState, Depends(get_state)]
StoreDep = Annotated[ConfigStore, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
PreviewDep = Annotated[PreviewService, Depends(get_preview)]
JobsDep = Annotated[JobManager, Depends(get_jobs)]
AudioPlayerDep = Annotated[AudioPlayer, Depends(get_audio_player)]
CatalogDep = Annotated[JobCatalog, Depends(get_catalog)]
BoardStoreDep = Annotated[BoardSettingsStore, Depends(get_board_store)]
