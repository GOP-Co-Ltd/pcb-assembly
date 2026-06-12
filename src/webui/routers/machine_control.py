"""マシン操作パネル（homing / ジョグ / 絶対移動 / relax / フォーカスZ）の API."""

from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pcbasm import gcode
from pcbasm.hal import XYZStage
from webui.app import StateDep
from webui.models import KlipperStatus
from webui.routers.system import create_klipper, fetch_status
from webui.state import AppState, BusyError

MOVE_TIMEOUT = 60.0  # wait_for_done (M400) を含むため長め

router = APIRouter(prefix="/api")


class MachineControlRequest(BaseModel):
    action: Literal["home", "jog", "move", "relax", "focus_z", "gcode"]
    axes: list[Literal["x", "y", "z"]] | None = None
    axis: Literal["x", "y", "z"] | None = None
    distance: float | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None
    gcode: str | None = None


@router.post("/machine-control")
def post_machine_control(body: MachineControlRequest, state: StateDep) -> KlipperStatus:
    try:
        with state.machine_lock("machine-control"):
            klipper = create_klipper(state, MOVE_TIMEOUT)
            stage = XYZStage(klipper.readonly)
            commands = _build_gcode(body, state, stage)
            klipper.send_gcode(commands)
            return fetch_status(klipper)
    except BusyError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=str(exc) or type(exc).__name__
        ) from exc
    except (RuntimeError, KeyError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _build_gcode(
    body: MachineControlRequest, state: AppState, stage: XYZStage
) -> gcode.GCode:
    """操作リクエストから送信する G-code を構築する.

    Raises:
        ValueError: パラメータ不足・limits 超過・フォーカスZ未設定の場合
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
    return commands + gcode.wait_for_done()
