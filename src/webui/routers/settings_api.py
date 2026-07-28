"""マシン設定の取得/保存 API."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from webui.config_store import MachineSettingValue
from webui.dependencies import JobsDep, StateDep, StoreDep
from webui.routers.common import SettingsField, machine_settings_fields

router = APIRouter(prefix="/api")


class MachineSettingsResponse(BaseModel):
    fields: list[SettingsField]


class SettingsUpdate(BaseModel):
    values: dict[str, MachineSettingValue]


@router.get("/settings/machine")
def get_machine_settings(store: StoreDep) -> MachineSettingsResponse:
    return MachineSettingsResponse(fields=machine_settings_fields(store))


@router.put("/settings/machine")
def put_machine_settings(
    body: SettingsUpdate, state: StateDep, store: StoreDep, jobs: JobsDep
) -> MachineSettingsResponse:
    with state.machine_lock("settings"):
        store.write_machine_settings(body.values)
        # crop はレンダラが毎フレーム読むためデバイス再構築は不要（ストリームを切断しない）
        if any(
            key.startswith("camera.") and not key.startswith("camera.crop.")
            for key in body.values
        ):
            state.rebuild_camera()
    jobs.publish_state_changed()
    return MachineSettingsResponse(fields=machine_settings_fields(store))
