"""マシン設定・モーション設定の取得/保存 API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from webui.app import SettingsDep, StateDep, StoreDep
from webui.config_store import MACHINE_FIELDS, MOTION_FIELDS, ConfigStore, FieldSpec
from webui.state import AppState

RESTART_TIMEOUT = 10.0

router = APIRouter(prefix="/api")


class SettingsField(BaseModel):
    key: str
    label: str
    value_type: Literal["float", "int", "str"]
    unit: str | None
    value: float | int | str | None


class MachineSettingsResponse(BaseModel):
    machine: str
    fields: list[SettingsField]


class SettingsUpdate(BaseModel):
    values: dict[str, float | int | str]


class MotionSettingsResponse(MachineSettingsResponse):
    symlink_ok: bool


class MotionUpdate(SettingsUpdate):
    restart: bool = False


class MotionUpdateResult(BaseModel):
    restart_requested: bool
    restart_ok: bool
    restart_error: str | None


def _fields(
    specs: tuple[FieldSpec, ...], values: Mapping[str, float | int | str | None]
) -> list[SettingsField]:
    return [
        SettingsField(
            key=spec.key,
            label=spec.label,
            value_type=spec.value_type,
            unit=spec.unit,
            value=values[spec.key],
        )
        for spec in specs
    ]


def machine_settings_fields(store: ConfigStore, machine: str) -> list[SettingsField]:
    """machine.toml のホワイトリスト項目を現在値付きで返す."""
    return _fields(MACHINE_FIELDS, store.read_machine_settings(machine))


def motion_settings_fields(store: ConfigStore, machine: str) -> list[SettingsField]:
    """printer.cfg のホワイトリスト項目を現在値付きで返す."""
    return _fields(MOTION_FIELDS, store.read_motion_settings(machine))


@router.get("/settings/machine")
def get_machine_settings(state: StateDep, store: StoreDep) -> MachineSettingsResponse:
    machine = state.selected_machine
    return MachineSettingsResponse(
        machine=machine, fields=machine_settings_fields(store, machine)
    )


@router.put("/settings/machine")
def put_machine_settings(
    body: SettingsUpdate, state: StateDep, store: StoreDep
) -> MachineSettingsResponse:
    machine = state.selected_machine
    with state.machine_lock("settings"):
        store.write_machine_settings(machine, body.values)
        if any(key.startswith("camera.") for key in body.values):
            state.rebuild_camera()
    return MachineSettingsResponse(
        machine=machine, fields=machine_settings_fields(store, machine)
    )


@router.get("/settings/motion")
def get_motion_settings(
    state: StateDep, store: StoreDep, settings: SettingsDep
) -> MotionSettingsResponse:
    machine = state.selected_machine
    return MotionSettingsResponse(
        machine=machine,
        fields=motion_settings_fields(store, machine),
        symlink_ok=store.symlink_points_to(machine, settings.printer_cfg_link),
    )


@router.put("/settings/motion")
def put_motion_settings(
    body: MotionUpdate, state: StateDep, store: StoreDep
) -> MotionUpdateResult:
    machine = state.selected_machine
    motion_values = _to_motion_values(body.values)
    with state.machine_lock("settings"):
        store.write_motion_settings(machine, motion_values)

    if not body.restart:
        return MotionUpdateResult(
            restart_requested=False, restart_ok=False, restart_error=None
        )

    error = _restart_klipper(state)
    return MotionUpdateResult(
        restart_requested=True, restart_ok=error is None, restart_error=error
    )


def _to_motion_values(values: dict[str, float | int | str]) -> dict[str, float]:
    """モーション設定値を float 辞書へ変換する.

    Raises:
        HTTPException: 数値以外の値が含まれる場合（400）
    """
    converted: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, (bool, str)):
            raise HTTPException(
                status_code=400,
                detail=f"{key}: 数値が必要です（与えられた値: {value!r}）",
            )
        converted[key] = float(value)
    return converted


def _restart_klipper(state: AppState) -> str | None:
    """Moonraker 経由で Klipper を RESTART する。失敗時はエラー文字列を返す."""
    klipper = state.machine().klipper
    url = f"http://{klipper.host}:{klipper.port}/printer/restart"
    try:
        response = httpx.post(url, timeout=RESTART_TIMEOUT)
    except httpx.HTTPError as exc:
        return str(exc) or type(exc).__name__
    if response.status_code >= 400:
        return f"{response.status_code} {response.reason_phrase}"
    return None
