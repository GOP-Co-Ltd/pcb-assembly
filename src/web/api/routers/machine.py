"""装置の API（操作パネルの命令・Klipper ステータス・可動域・ファームウェア再起動・緊急停止）.

命令は対話ジョブの command と同じ契約で受ける。
緊急停止の URL は更新経路の契約なので ``/api/machine`` の下へ移さず、別の router で ``/api/emergency-stop`` に置く。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi import APIRouter, HTTPException

from pcbasm.gcode import GCode
from pcbasm.hal import KlipperClient, XYZStage
from pcbasm.machine.commands import COMMAND_TIMEOUT, to_gcode
from web.api.deps import (
    ControlDep,
    DevicesDep,
    JobsDep,
    LockDep,
    MachineConfigDep,
    UpdateDep,
)
from web.api.errors import hal_errors_to_502
from web.api.jobs.manager import JobManager
from web.api.machine_commands import to_command
from web.contract.models import (
    FirmwareRestartResponse,
    KlipperStatus,
    MachineCommandRequest,
    Position,
)
from web.selfupdate.service import restart_scheduled_notice

STATUS_TIMEOUT = 10.0

router = APIRouter(prefix="/api/machine")
emergency_stop_router = APIRouter(prefix="/api")


def fetch_status(klipper: KlipperClient) -> KlipperStatus:
    """Klipper から位置と homed_axes を取得する。失敗時は connected=False."""
    try:
        position = klipper.get_status("gcode_move", "gcode_position")
        homed_axes = klipper.get_status("toolhead", "homed_axes")
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        return KlipperStatus(connected=False, error=str(exc) or type(exc).__name__)
    return KlipperStatus(
        connected=True,
        position=Position(x=position[0], y=position[1], z=position[2]),
        homed_axes=homed_axes,
    )


@router.post("/command")
def post_command(
    body: MachineCommandRequest,
    lock: LockDep,
    devices: DevicesDep,
    machine_config: MachineConfigDep,
    _control: ControlDep,
) -> KlipperStatus:
    """命令の G-code を Klipper へ送り、完了後の位置と homed_axes を返す.

    400: 可動域外・フォーカス Z 未設定・キャップ位置未記録 / 409: 装置が使用中 / 422: 命令の形の誤り /
    502: Klipper 通信エラー・printer.cfg の不備。
    """
    with lock.hold("machine-control"):
        try:
            with hal_errors_to_502():
                klipper = devices.klipper(COMMAND_TIMEOUT)
                commands = to_gcode(
                    to_command(body), XYZStage(klipper), machine_config.machine
                )
                klipper.send_gcode(commands)
                return fetch_status(klipper)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/status")
def get_status(devices: DevicesDep) -> KlipperStatus:
    """Klipper の位置と homed_axes を返す（不通でも 200 で ``connected=False``）."""
    return fetch_status(devices.klipper(STATUS_TIMEOUT))


@router.get("/limits")
def get_limits(devices: DevicesDep) -> dict[str, dict[str, float]]:
    """選択マシンの XYZ 可動域を返す（Moonraker 不通と printer.cfg の不備は 502）."""
    klipper = devices.klipper(STATUS_TIMEOUT)
    with hal_errors_to_502():
        limits = XYZStage(klipper).limits
    return {
        "x": {"min": limits.x.min, "max": limits.x.max},
        "y": {"min": limits.y.min, "max": limits.y.max},
        "z": {"min": limits.z.min, "max": limits.z.max},
    }


def _klipper_action(jobs: JobManager, action: Callable[[], object]) -> dict[str, bool]:
    """Abort 要求を立ててから Klipper へアクションを送る（不通は 502）."""
    # Klipper 送信が失敗しても abort フラグは必ず立てる（先頭で実行）
    jobs.request_abort()
    action()
    return {"ok": True}


@emergency_stop_router.post("/emergency-stop")
def post_emergency_stop(devices: DevicesDep, jobs: JobsDep) -> dict[str, bool]:
    """緊急停止（安全機能なので操作権でゲートしない。**ControlDep を足さない**）."""
    return _klipper_action(jobs, devices.emergency_stop)


@router.post("/firmware-restart")
def post_firmware_restart(
    devices: DevicesDep, jobs: JobsDep, update: UpdateDep, _control: ControlDep
) -> FirmwareRestartResponse:
    """ファームウェア再起動（復帰操作なので操作権が必要）.

    装置を立て直す操作なので、**起動中の pcbasm サービスも再起動する**。

    再起動するのは Klipper と backend WebAPI / UI frontend である。

    再起動対象と argv は更新時と同一なので、sudoers の追加設定は要らない。

    順序は「Klipper へ送る → unit を再起動」。逆にすると、Moonraker へ届かなかった
    場合でも画面だけが止まる。Klipper 送信が失敗した時点で 502 を返して中断する。
    """
    _klipper_action(
        jobs,
        lambda: devices.klipper(STATUS_TIMEOUT).send_gcode(GCode.firmware_restart()),
    )
    units = update.restart_units()
    warning = update.restart_services() or ""
    return FirmwareRestartResponse(
        message=(
            "ファームウェア再起動を送信しました。"
            + ("" if warning else restart_scheduled_notice(units))
        ),
        warning=warning,
    )
