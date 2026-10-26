"""マシン設定（machine.toml）の取得と保存の API."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from web.api.deps import ControlDep, DevicesDep, LockDep, MachineConfigDep
from web.api.machine_config import MachineConfigFile
from web.contract.models import MachineSettingsResponse, MachineSettingValue

router = APIRouter(prefix="/api/machine_config")


class SettingsUpdate(BaseModel):
    """machine.toml へ書き込む項目（キーはドット区切り。送ったキーだけを更新する）."""

    values: dict[str, MachineSettingValue]


def _settings(machine_config: MachineConfigFile) -> MachineSettingsResponse:
    machine, _ = machine_config.machine_or_error()
    return MachineSettingsResponse(fields=machine_config.fields(machine))


@router.get("")
def get_machine_settings(machine_config: MachineConfigDep) -> MachineSettingsResponse:
    """設定項目を、表示の定義・記載値（``value``）・実効値（``resolved``）付きで返す.

    設定エラー（degraded）の間は実効値を出さず、書かれている値だけで設定を直せるようにする。
    """
    return _settings(machine_config)


@router.put("")
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
