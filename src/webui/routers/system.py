"""Emergency Stop と Klipper ステータスの API."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from pcbasm.hal import Klipper, XYZStage
from webui.app import JobsDep, StateDep
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


def create_klipper(state: AppState, timeout: float) -> Klipper:
    """選択マシンの設定で Klipper クライアントを生成する."""
    klipper_config = state.machine().klipper
    return Klipper(host=klipper_config.host, port=klipper_config.port, timeout=timeout)


@router.get("/klipper/status")
def get_klipper_status(state: StateDep) -> KlipperStatus:
    return fetch_status(create_klipper(state, STATUS_TIMEOUT))


@router.get("/stage/limits")
def get_stage_limits(state: StateDep) -> dict[str, dict[str, float]]:
    """選択マシンの XYZ 可動域を返す（Moonraker 不通は 502）."""
    klipper = create_klipper(state, STATUS_TIMEOUT)
    try:
        limits = XYZStage(klipper.readonly).limits
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        raise HTTPException(
            status_code=502, detail=str(exc) or type(exc).__name__
        ) from exc
    return {
        "x": {"min": limits.x.min, "max": limits.x.max},
        "y": {"min": limits.y.min, "max": limits.y.max},
        "z": {"min": limits.z.min, "max": limits.z.max},
    }


@router.post("/emergency-stop")
def post_emergency_stop(state: StateDep, jobs: JobsDep) -> dict[str, bool]:
    # Klipper 送信が失敗しても abort フラグは必ず立てる（先頭で実行）
    jobs.request_abort()
    klipper = create_klipper(state, STATUS_TIMEOUT)
    try:
        klipper.emergency_stop()
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(
            status_code=502, detail=str(exc) or type(exc).__name__
        ) from exc
    return {"ok": True}
