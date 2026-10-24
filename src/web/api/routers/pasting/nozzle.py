"""ノズル保守位置（キャップ / クリーニング）の記録 API.

記録はどちらも「全軸ホーミング済みの現在のマシン座標を machine.toml へ保存する」点が
同じで、運転者から見ても同じ作業（ジョグで先端を当てて記録）なので 1 つの router に置く。

クリーニングのテスト実行はジョブ nozzle_clean_test が行う。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pcbasm.geometry import Point3d
from pcbasm.hal import XYZStage
from pcbasm.machine.devices import Devices
from web.api.deps import ControlDep, DevicesDep, LockDep, MachineConfigDep
from web.api.errors import hal_errors_to_502
from web.api.lock import MachineLock
from web.api.machine_config import MachineConfigFile
from web.api.routers.session import nozzle_clean_payload
from web.contract.models import NozzleCleanInfo

STATUS_TIMEOUT = 10.0  # 位置・homed_axes の読み取りのみ（移動なし）

router = APIRouter(prefix="/api/pasting/nozzle")


class NozzleCapPosition(BaseModel):
    x: float
    y: float
    z: float


def _record_current_position(
    lock: MachineLock,
    devices: Devices,
    machine_config: MachineConfigFile,
    *,
    owner: str,
    prefix: str,
) -> Point3d:
    """全軸ホーミングを検査し、現在位置を 3 桁丸めで ``prefix`` のテーブルへ書く.

    M84 後の座標は stale なので、未ホーミングのまま記録すると誤った位置が残る。

    Raises:
        HTTPException: xyz のいずれかが未ホーミングの場合（400）
    """
    with lock.hold(owner):
        with hal_errors_to_502():
            stage = XYZStage(devices.klipper(STATUS_TIMEOUT))
            if not stage.is_homed():
                raise HTTPException(
                    status_code=400, detail="全軸ホーミング後に記録してください"
                )
            position = stage.get_position()
        saved = Point3d(
            round(position.x, 3), round(position.y, 3), round(position.z, 3)
        )
        machine_config.write({f"{prefix}.position": [saved.x, saved.y, saved.z]})
    return saved


@router.post("/cap/record")
def record_nozzle_cap(
    lock: LockDep,
    devices: DevicesDep,
    machine_config: MachineConfigDep,
    _control: ControlDep,
) -> NozzleCapPosition:
    """現在のマシン座標をノズルキャップ位置として記録する."""
    saved = _record_current_position(
        lock,
        devices,
        machine_config,
        owner="nozzle-cap-record",
        prefix="paste_dispenser.nozzle_cap",
    )
    return NozzleCapPosition(x=saved.x, y=saved.y, z=saved.z)


@router.post("/clean/record")
def record_nozzle_clean(
    lock: LockDep,
    devices: DevicesDep,
    machine_config: MachineConfigDep,
    _control: ControlDep,
) -> NozzleCleanInfo:
    """現在のマシン座標をノズルクリーニング位置として記録する.

    記録するのはクリーニング面（先端が触れた高さ）で、押し込みは実行時に設定値ぶん差し引く。

    表示用の文字列も同じ応答で返す。
    """
    saved = _record_current_position(
        lock,
        devices,
        machine_config,
        owner="nozzle-clean-record",
        prefix="paste_dispenser.nozzle_clean",
    )
    return nozzle_clean_payload(saved)
