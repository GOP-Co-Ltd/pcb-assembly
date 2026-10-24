"""マシン操作パネルの API（命令は対話ジョブの command と同じ契約）."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pcbasm.hal import XYZStage
from pcbasm.machine.commands import COMMAND_TIMEOUT, to_gcode
from web.api.dependencies import ControlDep, StateDep
from web.api.errors import hal_errors_to_502
from web.api.machine_commands import to_command
from web.api.routers.common import fetch_status
from web.contract.models import KlipperStatus, MachineCommandRequest

router = APIRouter(prefix="/api")


@router.post("/machine-control")
def post_machine_control(
    body: MachineCommandRequest, state: StateDep, _control: ControlDep
) -> KlipperStatus:
    """命令の G-code を Klipper へ送り、完了後の位置と homed_axes を返す.

    400: 可動域外・フォーカス Z 未設定・キャップ位置未記録 / 409: 装置が使用中 / 422: 命令の形の誤り /
    502: Klipper 通信エラー・printer.cfg の不備。
    """
    with state.machine_lock("machine-control"):
        try:
            with hal_errors_to_502():
                klipper = state.devices.klipper(COMMAND_TIMEOUT)
                commands = to_gcode(to_command(body), XYZStage(klipper), state.machine)
                klipper.send_gcode(commands)
                return fetch_status(klipper)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
