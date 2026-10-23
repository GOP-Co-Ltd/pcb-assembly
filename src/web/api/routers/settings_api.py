"""マシン設定の取得/保存と、設定履歴の一覧/復元 API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web.api.dependencies import ControlDep, HistoryDep, JobsDep, StateDep, StoreDep
from web.api.history import UnknownSnapshotError
from web.api.models import (
    ConfigHistoryResponse,
    MachineSettingsResponse,
    MachineSettingValue,
)
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


@router.get("/settings/history")
def get_config_history(history: HistoryDep) -> ConfigHistoryResponse:
    """machine.toml の世代を新しい順に、次に入った変更の要約付きで返す."""
    return ConfigHistoryResponse(entries=history.entries())


@router.post("/settings/history/{name}/restore")
def restore_config_history(
    name: str,
    state: StateDep,
    history: HistoryDep,
    jobs: JobsDep,
    _control: ControlDep,
) -> ConfigHistoryResponse:
    """指定の世代を machine.toml へ書き戻し、書き戻した後の世代一覧を返す.

    書き戻す前の状態も世代に残る。カメラ設定も戻りうるので、カメラは常に再構築する。

    404: 世代が無い / 409: 装置が使用中。
    """
    with state.machine_lock("settings"):
        try:
            history.restore(name)
        except UnknownSnapshotError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        state.rebuild_camera()
    jobs.publish_state_changed()
    return ConfigHistoryResponse(entries=history.entries())
