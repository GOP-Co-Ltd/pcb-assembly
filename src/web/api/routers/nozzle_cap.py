"""ノズル保守位置（キャップ / クリーニング）の記録 API.

どちらも「全軸ホーミング済みの現在のマシン座標を machine.toml へ保存する」点は同じで、
運転者から見ても同じ作業（ジョグで先端を当てて記録）なので 1 つの router に置く。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pcbasm.geometry import Point3d
from pcbasm.hal import XYZStage
from web.api.config_store import ConfigStore
from web.api.dependencies import ControlDep, JobsDep, StateDep, StoreDep
from web.api.models import NozzleCleanInfo
from web.api.routers.common import (
    create_klipper,
    klipper_errors_to_502,
    nozzle_clean_payload,
)
from web.api.state import AppState

STATUS_TIMEOUT = 10.0  # 位置・homed_axes の読み取りのみ（移動なし）

router = APIRouter(prefix="/api")


class NozzleCapPosition(BaseModel):
    x: float
    y: float
    z: float


def _record_current_position(
    state: AppState, store: ConfigStore, *, owner: str, prefix: str
) -> Point3d:
    """全軸ホーミングを検査し、現在位置を 3 桁丸めで ``prefix`` のテーブルへ書く.

    ホーミングを検査するのは M84 後の stale 座標を記録させないため。
    """
    # BusyError（RuntimeError 派生）は 502 変換に巻き込まず app.py の 409 ハンドラへ
    # 流すため、machine_lock は klipper_errors_to_502 の外側で取る
    with state.machine_lock(owner):
        with klipper_errors_to_502():
            klipper = create_klipper(state, STATUS_TIMEOUT)
            homed_axes = klipper.get_status("toolhead", "homed_axes")
            if not all(axis in homed_axes for axis in "xyz"):
                raise HTTPException(
                    status_code=400, detail="全軸ホーミング後に記録してください"
                )
            position = XYZStage(klipper.readonly).get_position()
        saved = Point3d(
            round(position.x, 3), round(position.y, 3), round(position.z, 3)
        )
        store.write_machine_settings(
            {f"{prefix}.x": saved.x, f"{prefix}.y": saved.y, f"{prefix}.z": saved.z}
        )
    return saved


@router.post("/pasting/nozzle-cap/record")
def record_nozzle_cap(
    state: StateDep, store: StoreDep, jobs: JobsDep, _control: ControlDep
) -> NozzleCapPosition:
    """現在のマシン座標をノズルキャップ位置として記録する."""
    saved = _record_current_position(
        state, store, owner="nozzle-cap-record", prefix="nozzle_cap"
    )
    jobs.publish_state_changed()
    return NozzleCapPosition(x=saved.x, y=saved.y, z=saved.z)


@router.post("/pasting/nozzle-clean/record")
def record_nozzle_clean(
    state: StateDep, store: StoreDep, jobs: JobsDep, _control: ControlDep
) -> NozzleCleanInfo:
    """現在のマシン座標をノズルクリーニング位置として記録する.

    記録するのはクリーニング面（先端が触れた高さ）で、押し込みは実行時に設定値ぶん
    差し引く。書き込んだ設定を読み戻すので、押し込み後の高さと表示文字列も返せる。
    """
    _record_current_position(
        state, store, owner="nozzle-clean-record", prefix="nozzle_clean"
    )
    clean = state.nozzle_clean()
    if clean is None:
        raise HTTPException(
            status_code=500, detail="記録した設定を読み戻せませんでした"
        )
    jobs.publish_state_changed()
    return nozzle_clean_payload(clean)
