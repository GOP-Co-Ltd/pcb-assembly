"""Emergency Stop と Klipper ステータスの API."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter

from pcbasm.hal import Klipper, XYZStage
from web.api.dependencies import ControlDep, JobsDep, StateDep
from web.api.jobs.manager import JobManager
from web.api.models import KlipperStatus
from web.api.routers.common import create_klipper, fetch_status, klipper_errors_to_502
from web.api.state import AppState

STATUS_TIMEOUT = 10.0

router = APIRouter(prefix="/api")


@router.get("/klipper/status")
def get_klipper_status(state: StateDep) -> KlipperStatus:
    return fetch_status(create_klipper(state, STATUS_TIMEOUT))


@router.get("/stage/limits")
def get_stage_limits(state: StateDep) -> dict[str, dict[str, float]]:
    """選択マシンの XYZ 可動域を返す（Moonraker 不通は 502）."""
    klipper = create_klipper(state, STATUS_TIMEOUT)
    with klipper_errors_to_502():
        limits = XYZStage(klipper.readonly).limits
    return {
        "x": {"min": limits.x.min, "max": limits.x.max},
        "y": {"min": limits.y.min, "max": limits.y.max},
        "z": {"min": limits.z.min, "max": limits.z.max},
    }


def _klipper_action(
    state: AppState, jobs: JobManager, action: Callable[[Klipper], None]
) -> dict[str, bool]:
    """Abort 要求を立ててから Klipper へアクションを送る（不通は 502）."""
    # Klipper 送信が失敗しても abort フラグは必ず立てる（先頭で実行）
    jobs.request_abort()
    klipper = create_klipper(state, STATUS_TIMEOUT)
    with klipper_errors_to_502():
        action(klipper)
    return {"ok": True}


@router.post("/emergency-stop")
def post_emergency_stop(state: StateDep, jobs: JobsDep) -> dict[str, bool]:
    """緊急停止（安全機能なので操作権でゲートしない。**ControlDep を足さない**）."""
    return _klipper_action(state, jobs, Klipper.emergency_stop)


@router.post("/firmware-restart")
def post_firmware_restart(
    state: StateDep, jobs: JobsDep, _control: ControlDep
) -> dict[str, bool]:
    """ファームウェア再起動（復帰操作なので操作権が必要）."""
    return _klipper_action(state, jobs, Klipper.firmware_restart)
