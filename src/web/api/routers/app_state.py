"""アプリ状態と backend 自己申告の API."""

from __future__ import annotations

from fastapi import APIRouter

from web.api.dependencies import JobsDep, PreviewDep, SettingsDep, StateDep
from web.api.models import MachineInfo
from web.api.routers.common import (
    StateResponse,
    build_machine_info,
    build_state_response,
)

router = APIRouter(prefix="/api")


@router.get("/state")
def get_state(
    state: StateDep, settings: SettingsDep, preview: PreviewDep, jobs: JobsDep
) -> StateResponse:
    return build_state_response(state, settings, preview, jobs)


@router.get("/machine-info")
def get_machine_info(state: StateDep, settings: SettingsDep) -> MachineInfo:
    """Backend が自己申告する装置情報を返す（到達性プローブ兼用）."""
    return build_machine_info(state, settings)
