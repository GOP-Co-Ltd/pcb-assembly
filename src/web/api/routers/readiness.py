"""立ち上げ状況の API（各段が済んだかを読むだけで、装置には触れない）.

設定エラー（degraded）の間も 200 を返す。立ち上げの途中で machine.toml を壊しても、チェックリストを開ける。
"""

from __future__ import annotations

from fastapi import APIRouter

from pcbasm.commissioning import readiness
from web.api.deps import EnvDep, MachineConfigDep
from web.contract.models import ReadinessResponse

router = APIRouter(prefix="/api/readiness")


@router.get("")
def get_readiness(env: EnvDep, machine_config: MachineConfigDep) -> ReadinessResponse:
    """立ち上げの各段の状態を、手順の順に返す."""
    machine, _ = machine_config.machine_or_error()
    result = readiness(
        machine,
        machine_config.capabilities,
        env.machine_check_file,
        env.paste_volume_calibration_dir,
    )
    return ReadinessResponse.model_validate(result, from_attributes=True)
