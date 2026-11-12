"""装置の API（操作パネルの命令・Klipper ステータス・可動域・ファームウェア再起動・緊急停止）.

命令は対話ジョブの command と同じ契約で受ける。
XYZ を新ファームで動かす機体（machine.toml に ``[pnp.motion]``）では、同じ URL・同じ契約のまま、
Klipper の代わりに :class:`~pcbasm.hal.motion.MotionStage` で実行する。緊急停止の振り分けは
:meth:`~pcbasm.machine.devices.Devices.emergency_stop` が持つ。
緊急停止の URL は更新経路の契約なので ``/api/machine`` の下へ移さず、別の router で ``/api/emergency-stop`` に置く。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi import APIRouter, HTTPException

from pcbasm.config import ConfigError
from pcbasm.errors import DeviceError
from pcbasm.gcode import GCode
from pcbasm.geometry import Point3d
from pcbasm.hal import KlipperClient, MotionStage, MotionState, XYZStage
from pcbasm.machine.commands import COMMAND_TIMEOUT, to_gcode
from pcbasm.machine.devices import Devices
from pcbasm.machine.labels import position_label
from pcbasm.machine.motion_commands import (
    execute_motion_command,
    motion_homed_axes,
    motion_load_label,
    motion_position_label,
)
from web.api.deps import (
    ControlDep,
    DevicesDep,
    JobsDep,
    LockDep,
    MachineConfigDep,
    UpdateDep,
)
from web.api.errors import hal_errors_to_502
from web.api.jobs.runtime.manager import JobManager
from web.api.machine_commands import to_command
from web.contract.models import (
    FirmwareRestartResponse,
    KlipperStatus,
    MachineCommandRequest,
    Position,
)
from web.selfupdate.service import restart_scheduled_notice

STATUS_TIMEOUT = 10.0

# 新ファームの機体で、まだステージを開いていないときの status の理由
MOTION_NOT_OPENED = "まだ接続していません（原点出しなどの操作をすると接続します）"
# 新ファームの機体のファームウェア再起動の応答
MOTION_RESTARTED = "XYZ の MCU を再起動しました。原点は「未」に戻ったので、原点出しからやり直してください。"

router = APIRouter(prefix="/api/machine")
emergency_stop_router = APIRouter(prefix="/api")


def fetch_status(klipper: KlipperClient) -> KlipperStatus:
    """Klipper から位置と homed_axes を取得する。失敗時は connected=False."""
    try:
        position = klipper.get_status("gcode_move", "gcode_position")
        homed_axes = klipper.get_status("toolhead", "homed_axes")
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        return KlipperStatus(connected=False, error=str(exc) or type(exc).__name__)
    point = Point3d(position[0], position[1], position[2])
    return KlipperStatus(
        connected=True,
        position=Position.model_validate(point, from_attributes=True),
        position_label=position_label(point),
        homed_axes=homed_axes,
    )


def fetch_motion_status(stage: MotionStage | None) -> KlipperStatus:
    """新ファームのステージの位置と原点出しの済んだ軸と荷重を返す（開いていない・話せない・shutdown は
    connected=False）."""
    if stage is None:
        return KlipperStatus(connected=False, error=MOTION_NOT_OPENED)
    try:
        status = stage.status()
    except DeviceError as exc:
        return KlipperStatus(connected=False, error=str(exc) or type(exc).__name__)
    if status.state is MotionState.SHUTDOWN:
        return KlipperStatus(
            connected=False,
            error=f"MCU が止まっています（{status.shutdown_reason}）。「再起動（復旧）」で戻します",
        )
    return KlipperStatus(
        connected=True,
        position=Position.model_validate(status.position, from_attributes=True),
        position_label=motion_position_label(status),
        homed_axes=motion_homed_axes(status),
        load_label=motion_load_label(status),
    )


def _run_motion_command(body: MachineCommandRequest, devices: Devices) -> KlipperStatus:
    """命令を新ファームのステージで実行し、終わった後の状態を返す（実行できない命令は 400）."""
    stage = devices.motion_stage()
    try:
        execute_motion_command(to_command(body), stage)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return fetch_motion_status(stage)


@router.post("/command")
def post_command(
    body: MachineCommandRequest,
    lock: LockDep,
    devices: DevicesDep,
    machine_config: MachineConfigDep,
    _control: ControlDep,
) -> KlipperStatus:
    """命令の G-code を Klipper へ送り、完了後の位置と homed_axes を返す.

    400: 可動域外・フォーカス Z 未設定・キャップ位置未記録・タール（新ファームの機体だけ） / 409: 装置が使用中 / 422: 命令の形の誤り /
    502: Klipper 通信エラー・printer.cfg の不備。

    新ファームの機体ではステージで実行する（焦点 Z・キャップ位置・任意 G-code と、ロードセルが無いか値が来ていない
    ときのタールは 400。
    MCU と話せない・shutdown は 502）。
    """
    with lock.hold("machine-control"):
        if devices.uses_motion_stage():
            return _run_motion_command(body, devices)
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
    """Klipper の位置と homed_axes を返す（不通でも 200 で ``connected=False``）.

    新ファームの機体では、開いているステージだけを読む（ポーリングで MCU に接続しない）。 machine.toml
    が読めないときは、どちらの機体でも設定エラー（503）にする。
    """
    if devices.uses_motion_stage():
        return fetch_motion_status(devices.opened_motion_stage())
    return fetch_status(devices.klipper(STATUS_TIMEOUT))


@router.get("/limits")
def get_limits(devices: DevicesDep) -> dict[str, dict[str, float]]:
    """選択マシンの XYZ 可動域を返す（Moonraker 不通と printer.cfg の不備は 502）.

    新ファームの機体では [pnp.motion] の可動域を返す（ページを開くたびに呼ばれるので、MCU に接続しない）。
    """
    if devices.uses_motion_stage():
        motion_limits = devices.motion_limits()
        return {
            axis: {
                "min": getattr(motion_limits, axis).min,
                "max": getattr(motion_limits, axis).max,
            }
            for axis in ("x", "y", "z")
        }
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

    新ファームの機体では、ステージを立て直す（MCU を reset して設定し直す）だけで、サービスは再起動しない。
    ジョブが原点出しや移動の最中なら、それが終わってから立て直す。
    """
    try:
        motion = devices.uses_motion_stage()
    except ConfigError:
        motion = False  # Klipper の経路が、中止を立ててから設定エラーを返す
    if motion:
        jobs.request_abort()
        devices.restart_motion_stage()
        return FirmwareRestartResponse(message=MOTION_RESTARTED)
    _klipper_action(
        jobs,
        lambda: devices.klipper(STATUS_TIMEOUT).send_gcode(GCode.firmware_restart()),
    )
    units = update.restart_units()
    warning = update.restart_services() or ""
    return FirmwareRestartResponse(
        message=(
            "再起動（復旧）を送信しました。"
            + ("" if warning else restart_scheduled_notice(units))
        ),
        warning=warning,
    )
