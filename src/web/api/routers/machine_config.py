"""マシン設定の取得/保存と、設定履歴の一覧/復元 API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web.api.deps import ControlDep, DevicesDep, LockDep, MachineConfigDep
from web.api.history import UnknownSnapshotError
from web.api.machine_config import MachineConfigFile
from web.contract.models import (
    ConfigHistoryResponse,
    MachineSettingsResponse,
    MachineSettingValue,
)

router = APIRouter(prefix="/api")


class SettingsUpdate(BaseModel):
    """machine.toml へ書き込む項目（キーはドット区切り。送ったキーだけを更新する）."""

    values: dict[str, MachineSettingValue]


def _settings(machine_config: MachineConfigFile) -> MachineSettingsResponse:
    machine, _ = machine_config.machine_or_error()
    return MachineSettingsResponse(fields=machine_config.fields(machine))


@router.get("/settings/machine")
def get_machine_settings(machine_config: MachineConfigDep) -> MachineSettingsResponse:
    """設定項目を、表示の定義・記載値（``value``）・実効値（``resolved``）付きで返す.

    設定エラー（degraded）の間は実効値を出さず、書かれている値だけで設定を直せるようにする。
    """
    return _settings(machine_config)


@router.put("/settings/machine")
def put_machine_settings(
    body: SettingsUpdate,
    lock: LockDep,
    devices: DevicesDep,
    machine_config: MachineConfigDep,
    _control: ControlDep,
) -> MachineSettingsResponse:
    """machine.toml の項目を保存し、保存後の全項目を返す.

    400: 未知キー・型不一致・相互制約違反（1 件でも不正なら何も書かない）/ 409: 装置が使用中。
    ``camera.*``（``camera.crop.*`` を除く）を変えたときはカメラを再構築する。
    """
    with lock.hold("settings"):
        machine_config.write(body.values)
        # crop はレンダラが毎フレーム読むためデバイス再構築は不要（ストリームを切断しない）
        if any(
            key.startswith("camera.") and not key.startswith("camera.crop.")
            for key in body.values
        ):
            devices.rebuild_camera()
    return _settings(machine_config)


@router.get("/settings/history")
def get_config_history(machine_config: MachineConfigDep) -> ConfigHistoryResponse:
    """machine.toml の世代を新しい順に、次に入った変更の要約付きで返す."""
    return ConfigHistoryResponse(entries=machine_config.history_entries())


@router.post("/settings/history/{name}/restore")
def restore_config_history(
    name: str,
    lock: LockDep,
    devices: DevicesDep,
    machine_config: MachineConfigDep,
    _control: ControlDep,
) -> ConfigHistoryResponse:
    """指定の世代を machine.toml へ書き戻し、書き戻した後の世代一覧を返す.

    書き戻す前の状態も世代に残る。カメラ設定も戻りうるので、カメラは常に再構築する。

    404: 世代が無い / 409: 装置が使用中。
    """
    with lock.hold("settings"):
        try:
            machine_config.restore(name)
        except UnknownSnapshotError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        devices.rebuild_camera()
    return ConfigHistoryResponse(entries=machine_config.history_entries())
