"""マシン設定の取得/保存 API."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from web.api.config_store import MachineSettingValue
from web.api.dependencies import ControlDep, JobsDep, StateDep, StoreDep
from web.api.models import MachineSettingsResponse
from web.api.routers.common import machine_settings_fields

router = APIRouter(prefix="/api")


class SettingsUpdate(BaseModel):
    """machine.toml へ書き込む項目（キーはドット区切り。送ったキーだけを更新する）."""

    values: dict[str, MachineSettingValue]


@router.get("/settings/machine")
def get_machine_settings(state: StateDep, store: StoreDep) -> MachineSettingsResponse:
    """編集できる machine.toml の項目を、記載値（``value``）と実効値（``resolved``）付きで返す."""
    return MachineSettingsResponse(fields=machine_settings_fields(store, state))


@router.put("/settings/machine")
def put_machine_settings(
    body: SettingsUpdate,
    state: StateDep,
    store: StoreDep,
    jobs: JobsDep,
    _control: ControlDep,
) -> MachineSettingsResponse:
    """machine.toml の項目を保存し、保存後の全項目を返す.

    400: 未知キー・型不一致・相互制約違反（1 件でも不正なら何も書かない）/ 409: 装置が使用中。
    ``camera.*``（``camera.crop.*`` を除く）を変えたときはカメラを再構築する。
    """
    with state.machine_lock("settings"):
        store.write_machine_settings(body.values)
        # crop はレンダラが毎フレーム読むためデバイス再構築は不要（ストリームを切断しない）
        if any(
            key.startswith("camera.") and not key.startswith("camera.crop.")
            for key in body.values
        ):
            state.rebuild_camera()
    jobs.publish_state_changed()
    return MachineSettingsResponse(fields=machine_settings_fields(store, state))
