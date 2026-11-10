"""ピンセットのサーボの API（マシン操作パネルの「サーボ脱力」）."""

from __future__ import annotations

from fastapi import APIRouter

from web.api.deps import ControlDep, DevicesDep, LockDep

router = APIRouter(prefix="/api/pnp/servo", tags=["pnp"])


@router.post("/relax")
def post_relax(
    lock: LockDep, devices: DevicesDep, _control: ControlDep
) -> dict[str, bool]:
    """バスにつながる全サーボのトルクを切る（[pnp.servo] の役割を設定する前でも届く）.

    409: 装置が使用中 / 423: 操作権が無い / 502: サーボのポートを開けない・送れない /
    503: [pnp.servo] が無い（設定エラー）。
    """
    with lock.hold("servo-relax"):
        devices.servo_bus().release_all()
    return {"ok": True}
