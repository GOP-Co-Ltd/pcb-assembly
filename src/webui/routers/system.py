"""Emergency Stop と Klipper ステータスの API."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from pcbasm.hal import Klipper
from webui.app import StateDep
from webui.models import KlipperStatus, Position
from webui.state import AppState

STATUS_TIMEOUT = 10.0

router = APIRouter(prefix="/api")


def fetch_status(klipper: Klipper) -> KlipperStatus:
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


def _create_klipper(state: AppState, timeout: float) -> Klipper:
    klipper_config = state.machine().klipper
    return Klipper(host=klipper_config.host, port=klipper_config.port, timeout=timeout)


@router.get("/klipper/status")
def get_klipper_status(state: StateDep) -> KlipperStatus:
    return fetch_status(_create_klipper(state, STATUS_TIMEOUT))


@router.post("/emergency-stop")
def post_emergency_stop(state: StateDep) -> dict[str, bool]:
    klipper = _create_klipper(state, STATUS_TIMEOUT)
    try:
        klipper.emergency_stop()
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(
            status_code=502, detail=str(exc) or type(exc).__name__
        ) from exc
    return {"ok": True}
