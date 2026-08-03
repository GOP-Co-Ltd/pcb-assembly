"""マシン設定の取得/保存 API."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from web.api.config_store import MachineSettingValue
from web.api.dependencies import AdvertiserDep, JobsDep, StateDep, StoreDep
from web.api.models import MachineSettingsResponse
from web.api.routers.common import machine_settings_fields

router = APIRouter(prefix="/api")


class SettingsUpdate(BaseModel):
    values: dict[str, MachineSettingValue]


@router.get("/settings/machine")
def get_machine_settings(state: StateDep, store: StoreDep) -> MachineSettingsResponse:
    return MachineSettingsResponse(fields=machine_settings_fields(store, state))


@router.put("/settings/machine")
def put_machine_settings(
    body: SettingsUpdate,
    state: StateDep,
    store: StoreDep,
    jobs: JobsDep,
    advertiser: AdvertiserDep,
) -> MachineSettingsResponse:
    name_before = state.machine_name()
    with state.machine_lock("settings"):
        store.write_machine_settings(body.values)
        # crop はレンダラが毎フレーム読むためデバイス再構築は不要（ストリームを切断しない）
        if any(
            key.startswith("camera.") and not key.startswith("camera.crop.")
            for key in body.values
        ):
            state.rebuild_camera()
    jobs.publish_state_changed()
    # 表示名が変わったら広告も更新する（更新しないと frontend のドロップダウンに
    # 古い名前が最大 75 分（PTR の other-TTL）残る）
    name_after = state.machine_name()
    if advertiser is not None and name_after != name_before:
        advertiser.update(name_after)
    return MachineSettingsResponse(fields=machine_settings_fields(store, state))
