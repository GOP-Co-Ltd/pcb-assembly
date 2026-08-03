"""Emergency Stop と Klipper ステータスの API."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter

from pcbasm.hal import Klipper, create_xyz_stage
from webui.dependencies import JobsDep, StateDep
from webui.jobs.manager import JobManager
from webui.models import KlipperStatus
from webui.routers.common import create_klipper, fetch_status, klipper_errors_to_502
from webui.state import AppState

STATUS_TIMEOUT = 10.0

router = APIRouter(prefix="/api")


@router.get("/klipper/status")
def get_klipper_status(state: StateDep) -> KlipperStatus:
    klipper = create_klipper(state, STATUS_TIMEOUT)
    try:
        stage = create_xyz_stage(state.machine(), klipper.readonly)
    except (OSError, ValueError) as exc:
        return KlipperStatus(connected=False, error=str(exc) or type(exc).__name__)
    return fetch_status(klipper, stage)


@router.get("/stage/limits")
def get_stage_limits(state: StateDep) -> dict[str, dict[str, float]]:
    """選択マシンの XYZ 可動域を返す（Moonraker 不通は 502）."""
    klipper = create_klipper(state, STATUS_TIMEOUT)
    with klipper_errors_to_502():
        limits = create_xyz_stage(state.machine(), klipper.readonly).limits
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
    return _klipper_action(state, jobs, Klipper.emergency_stop)


@router.post("/firmware-restart")
def post_firmware_restart(state: StateDep, jobs: JobsDep) -> dict[str, bool]:
    return _klipper_action(state, jobs, Klipper.firmware_restart)
