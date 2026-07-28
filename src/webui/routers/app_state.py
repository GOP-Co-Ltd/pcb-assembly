"""アプリ状態の API."""

from __future__ import annotations

from fastapi import APIRouter

from webui.dependencies import JobsDep, PreviewDep, SettingsDep, StateDep
from webui.routers.common import StateResponse, build_state_response

router = APIRouter(prefix="/api")


@router.get("/state")
def get_state(
    state: StateDep, settings: SettingsDep, preview: PreviewDep, jobs: JobsDep
) -> StateResponse:
    return build_state_response(state, settings, preview, jobs)
