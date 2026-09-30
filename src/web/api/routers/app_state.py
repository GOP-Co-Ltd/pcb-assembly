"""アプリ状態と backend 自己申告の API."""

from __future__ import annotations

from fastapi import APIRouter

from web.api.dependencies import (
    IdentityDep,
    JobsDep,
    LeaseDep,
    PreviewDep,
    SettingsDep,
    StateDep,
)
from web.api.models import MachineInfo
from web.api.routers.common import (
    StateResponse,
    build_machine_info,
    build_state_response,
)

router = APIRouter(prefix="/api")


@router.get("/state")
def get_state(
    state: StateDep,
    settings: SettingsDep,
    preview: PreviewDep,
    jobs: JobsDep,
    lease: LeaseDep,
    identity: IdentityDep,
) -> StateResponse:
    """選択 PCB・装置の使用状況・直近ジョブ・操作権などのアプリ状態を返す.

    閲覧は自由なので操作権でゲートしない。
    ``control.key`` と ``you.key`` が一致すれば、リクエスト元が操作権の保持者である。
    """
    return build_state_response(
        state, settings, preview, jobs, lease.snapshot(), identity
    )


@router.get("/machine-info")
def get_machine_info(state: StateDep, settings: SettingsDep) -> MachineInfo:
    """Backend が自己申告する装置情報を返す（到達性プローブ兼用）."""
    return build_machine_info(state, settings)
