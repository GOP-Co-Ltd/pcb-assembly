"""マシン操作パネル（homing / ジョグ / 絶対移動 / relax / フォーカスZ / キャップ移動）の API."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pcbasm import gcode
from pcbasm.hal import XYZStage
from pcbasm.parking import move_to_cap
from web.api.dependencies import StateDep
from web.api.models import KlipperStatus
from web.api.routers.common import create_klipper, fetch_status, klipper_errors_to_502
from web.api.state import AppState

MOVE_TIMEOUT = 60.0  # wait_for_done (M400) を含むため長め

router = APIRouter(prefix="/api")


class MachineControlRequest(BaseModel):
    action: Literal["home", "jog", "move", "relax", "focus_z", "gcode", "move_to_cap"]
    axes: list[Literal["x", "y", "z"]] | None = None
    axis: Literal["x", "y", "z"] | None = None
    distance: float | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None
    gcode: str | None = None


@router.post("/machine-control")
def post_machine_control(body: MachineControlRequest, state: StateDep) -> KlipperStatus:
    # BusyError（RuntimeError 派生）は 502 変換に巻き込まず app.py の 409 ハンドラへ
    # 流すため、machine_lock は klipper_errors_to_502 の外側で取る
    with state.machine_lock("machine-control"):
        try:
            with klipper_errors_to_502():
                klipper = create_klipper(state, MOVE_TIMEOUT)
                stage = XYZStage(klipper.readonly)
                commands = _build_gcode(body, state, stage)
                klipper.send_gcode(commands)
                return fetch_status(klipper)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _build_gcode(
    body: MachineControlRequest, state: AppState, stage: XYZStage
) -> gcode.GCode:
    """操作リクエストから送信する G-code を構築する.

    Raises:
        ValueError: パラメータ不足・limits 超過・フォーカスZ未設定・
            キャップ位置未記録の場合
    """
    match body.action:
        case "home":
            axes = body.axes or []
            commands = gcode.homing(x="x" in axes, y="y" in axes, z="z" in axes)
        case "jog":
            if body.axis is None or body.distance is None:
                raise ValueError("jog には axis と distance が必要です")
            commands = stage.move(
                x=body.distance if body.axis == "x" else None,
                y=body.distance if body.axis == "y" else None,
                z=body.distance if body.axis == "z" else None,
                relative=True,
            )
        case "move":
            commands = stage.move(x=body.x, y=body.y, z=body.z)
        case "relax":
            return gcode.relax()
        case "gcode":
            if body.gcode is None or not body.gcode.strip():
                raise ValueError("gcode が空です")
            commands = gcode.GCode(body.gcode)
        case "focus_z":
            focus_z = state.focus_z()
            if focus_z is None:
                raise ValueError(
                    "フォーカスZが取得できません（カメラキャリブレーション未設定）"
                )
            commands = stage.move(z=focus_z)
        case "move_to_cap":
            cap = state.nozzle_cap()
            if cap is None:
                raise ValueError("ノズルキャップ位置が未記録です")
            commands = move_to_cap(stage, cap)
    return commands + gcode.wait_for_done()
