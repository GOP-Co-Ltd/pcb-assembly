"""ノズルキャップ位置の記録 API（現在のマシン座標を machine.toml へ保存する）."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pcbasm.hal import XYZStage
from webui.dependencies import JobsDep, StateDep, StoreDep
from webui.routers.common import create_klipper, klipper_errors_to_502

STATUS_TIMEOUT = 10.0  # 位置・homed_axes の読み取りのみ（移動なし）

router = APIRouter(prefix="/api")


class NozzleCapPosition(BaseModel):
    x: float
    y: float
    z: float


@router.post("/pasting/nozzle-cap/record")
def record_nozzle_cap(
    state: StateDep, store: StoreDep, jobs: JobsDep
) -> NozzleCapPosition:
    """現在のマシン座標をノズルキャップ位置として記録する.

    全軸ホーミング済みであることを検査してから、現在位置を 3 桁丸めで machine.toml の [nozzle_cap]
    へ書き込む（M84 後の stale 座標記録防止）。
    """
    # BusyError（RuntimeError 派生）は 502 変換に巻き込まず app.py の 409 ハンドラへ
    # 流すため、machine_lock は klipper_errors_to_502 の外側で取る
    with state.machine_lock("nozzle-cap-record"):
        with klipper_errors_to_502():
            klipper = create_klipper(state, STATUS_TIMEOUT)
            homed_axes = klipper.get_status("toolhead", "homed_axes")
            if not all(axis in homed_axes for axis in "xyz"):
                raise HTTPException(
                    status_code=400, detail="全軸ホーミング後に記録してください"
                )
            position = XYZStage(klipper.readonly).get_position()
        saved = NozzleCapPosition(
            x=round(position.x, 3), y=round(position.y, 3), z=round(position.z, 3)
        )
        store.write_machine_settings(
            {"nozzle_cap.x": saved.x, "nozzle_cap.y": saved.y, "nozzle_cap.z": saved.z}
        )
    jobs.publish_state_changed()
    return saved
