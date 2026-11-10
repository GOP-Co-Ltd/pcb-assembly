"""ピンセットのサーボの API（マシン操作パネルの「サーボ脱力」とピンセット欄）."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from web.api.deps import ControlDep, DevicesDep, LockDep

router = APIRouter(prefix="/api/pnp/servo", tags=["pnp"])


class RotateRequest(BaseModel):
    """Rotation を回す角度（正負で向き）."""

    degrees: float = Field(ge=-360, le=360)


class OpenRequest(BaseModel):
    """Open Close の開き具合（0=閉じ切り、1=開き切り）."""

    fraction: float = Field(ge=0, le=1)


class TweezerStatus(BaseModel):
    """ピンセットの今の状態.

    Attributes:
        open_fraction: 今の位置の開き具合（実測）
        target_fraction: Open Close の goal の開き具合（最後に送った目標。つまんで止まっていても変わらない）
    """

    open_fraction: float
    target_fraction: float


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


@router.post("/rotate")
def post_rotate(
    body: RotateRequest, lock: LockDep, devices: DevicesDep, _control: ControlDep
) -> dict[str, bool]:
    """Rotation を degrees だけ相対に、[pnp.servo] の rotation_speed で回す（完了は待たない）.

    409: 装置が使用中 / 422: 範囲外 / 423: 操作権が無い / 502: サーボが応答しない・モードか角度制限が合わない /
    503: [pnp.servo] か役割・開閉の角度が無い（設定エラー）。
    """
    with lock.hold("servo-rotate"):
        devices.tweezer().rotate_by(body.degrees)
    return {"ok": True}


@router.post("/open")
def post_open(
    body: OpenRequest, lock: LockDep, devices: DevicesDep, _control: ControlDep
) -> dict[str, bool]:
    """Open Close を開き具合 fraction の位置へ動かす（完了は待たない）.

    エラーは :func:`post_rotate` と同じ。
    """
    with lock.hold("servo-open"):
        devices.tweezer().open_to(body.fraction)
    return {"ok": True}


@router.get("/status")
def get_status(devices: DevicesDep) -> TweezerStatus:
    """今の開き具合と目標の開き具合を返す（トルクには触れない。ジョブの実行中も読める）.

    502: サーボが応答しない・モードか角度制限が合わない / 503: [pnp.servo] か役割・開閉の角度が無い。
    """
    tweezer = devices.tweezer()
    return TweezerStatus(
        open_fraction=tweezer.open_fraction(), target_fraction=tweezer.target_fraction()
    )
