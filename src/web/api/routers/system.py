"""Emergency Stop・ファームウェア再起動・Klipper ステータスの API."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter

from pcbasm.hal import Klipper, XYZStage
from web.api.dependencies import ControlDep, JobsDep, StateDep, UpdateDep
from web.api.jobs.manager import JobManager
from web.api.models import FirmwareRestartResponse, KlipperStatus
from web.api.routers.common import create_klipper, fetch_status, klipper_errors_to_502
from web.api.state import AppState
from web.selfupdate.service import restart_scheduled_notice

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
    state: StateDep, jobs: JobsDep, update: UpdateDep, _control: ControlDep
) -> FirmwareRestartResponse:
    """ファームウェア再起動（復帰操作なので操作権が必要）.

    装置を立て直す操作なので、**Klipper だけでなく起動中の pcbasm サービス （backend WebAPI / UI
    frontend）も再起動する**。再起動対象と argv は更新時と 同一なので sudoers の追加設定は要らない。

    順序は「Klipper へ送る → unit を再起動」。逆にすると、Moonraker へ届かなかった
    場合でも画面だけが落ちる。Klipper 送信が失敗した時点で 502 を返して中断する。
    """
    _klipper_action(state, jobs, Klipper.firmware_restart)
    units = update.restart_units()
    warning = update.restart_services() or ""
    return FirmwareRestartResponse(
        message=(
            "ファームウェア再起動を送信しました。"
            + ("" if warning else restart_scheduled_notice(units))
        ),
        warning=warning,
    )
