"""マシン設定の取得/保存 API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from webui.app import JobsDep, StateDep, StoreDep
from webui.config_store import (
    MACHINE_FIELDS,
    ConfigStore,
    FieldSpec,
    MachineSettingValue,
)

router = APIRouter(prefix="/api")


class SettingsField(BaseModel):
    key: str
    label: str
    value_type: Literal["float", "int", "str", "float_or_auto", "dispense_mode", "bool"]
    unit: str | None
    value: MachineSettingValue | None


class MachineSettingsResponse(BaseModel):
    machine: str
    fields: list[SettingsField]


class SettingsUpdate(BaseModel):
    values: dict[str, MachineSettingValue]


def _fields(
    specs: tuple[FieldSpec, ...], values: Mapping[str, MachineSettingValue | None]
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


@router.get("/settings/machine")
def get_machine_settings(state: StateDep, store: StoreDep) -> MachineSettingsResponse:
    machine = state.selected_machine
    return MachineSettingsResponse(
        machine=machine, fields=machine_settings_fields(store, machine)
    )


@router.put("/settings/machine")
def put_machine_settings(
    body: SettingsUpdate, state: StateDep, store: StoreDep, jobs: JobsDep
) -> MachineSettingsResponse:
    machine = state.selected_machine
    with state.machine_lock("settings"):
        store.write_machine_settings(machine, body.values)
        if any(key.startswith("camera.") for key in body.values):
            state.rebuild_camera()
    jobs.publish_state_changed()
    return MachineSettingsResponse(
        machine=machine, fields=machine_settings_fields(store, machine)
    )
