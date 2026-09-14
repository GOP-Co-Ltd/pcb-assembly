"""ノズル保守位置（キャップ / クリーニング）の記録 API とクリーニングのテスト実行.

記録はどちらも「全軸ホーミング済みの現在のマシン座標を machine.toml へ保存する」点が
同じで、運転者から見ても同じ作業（ジョグで先端を当てて記録）なので 1 つの router に置く。
クリーニングのテスト実行も同じ画面で位置と押し込み量を追い込むための操作なのでここに置く。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pcbasm.geometry import Point3d
from pcbasm.hal import Klipper, XYZStage
from pcbasm.pasting.applicator import build_applicator
from pcbasm.pasting.nozzle_clean import clean_nozzle
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

# パージ（retract_rate での押出）と十字往復の M400 待ちを含むので長め。
# frontend の proxy_read_timeout（120 秒）より短くして、打ち切りの主が backend 側に残るようにする
CLEAN_TIMEOUT = 90.0

router = APIRouter(prefix="/api")


class NozzleCapPosition(BaseModel):
    x: float
    y: float
    z: float


class NozzleCleanTestResult(BaseModel):
    """テスト実行の結果（実施内容の 1 行。組み立てはサーバー側）."""

    message: str


def _require_all_axes_homed(klipper: Klipper, detail: str) -> None:
    """全軸ホーミング済みでなければ ``detail`` を添えて 400 で断る.

    M84 後の座標は stale なので、記録すれば誤った位置が残り、絶対移動を送れば
    意図しない場所へ動く。

    Raises:
        HTTPException: xyz のいずれかが未ホーミングの場合（400）
    """
    homed_axes = klipper.get_status("toolhead", "homed_axes")
    if not all(axis in homed_axes for axis in "xyz"):
        raise HTTPException(status_code=400, detail=detail)


def _record_current_position(
    state: AppState, store: ConfigStore, *, owner: str, prefix: str
) -> Point3d:
    """全軸ホーミングを検査し、現在位置を 3 桁丸めで ``prefix`` のテーブルへ書く."""
    # BusyError（RuntimeError 派生）は 502 変換に巻き込まず app.py の 409 ハンドラへ
    # 流すため、machine_lock は klipper_errors_to_502 の外側で取る
    with state.machine_lock(owner):
        with klipper_errors_to_502():
            klipper = create_klipper(state, STATUS_TIMEOUT)
            _require_all_axes_homed(klipper, "全軸ホーミング後に記録してください")
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
        state, store, owner="nozzle-cap-record", prefix="paste_dispenser.nozzle_cap"
    )
    jobs.publish_state_changed()
    return NozzleCapPosition(x=saved.x, y=saved.y, z=saved.z)


@router.post("/pasting/nozzle-clean/record")
def record_nozzle_clean(
    state: StateDep, store: StoreDep, jobs: JobsDep, _control: ControlDep
) -> NozzleCleanInfo:
    """現在のマシン座標をノズルクリーニング位置として記録する.

    記録するのはクリーニング面（先端が触れた高さ）で、押し込みは実行時に設定値ぶん
    差し引く。書き込んだ設定を読み戻すので、表示用の文字列も同じ応答で返せる。
    """
    _record_current_position(
        state, store, owner="nozzle-clean-record", prefix="paste_dispenser.nozzle_clean"
    )
    # 書き込みは終わっているので、読み戻しの成否に関わらず状態変更を知らせる
    jobs.publish_state_changed()
    clean = state.nozzle_clean()
    if clean is None:
        raise HTTPException(
            status_code=500, detail="記録した設定を読み戻せませんでした"
        )
    return nozzle_clean_payload(clean)


@router.post("/pasting/nozzle-clean/test")
def run_nozzle_clean_test(
    state: StateDep, _control: ControlDep
) -> NozzleCleanTestResult:
    """記録済みの設定でクリーニング動作を 1 回実行する（ブロッキング）.

    塗布ジョブが行うのと同じ手順（接近 → パージ → 十字往復 → 退避）を走らせ、続けて
    リトラクトする。ジョブでは :func:`clean_nozzle` の直後に
    :meth:`PasteApplicator.retract` が呼ばれるので、テストも同じ正味の状態で終える。

    Raises:
        HTTPException: 位置が未記録・未ホーミング・可動域外（400）、Klipper 不達（502）
    """
    # 装置排他を先に取る。設定を読むだけの検査より「他が使用中」のほうが行動可能
    with state.machine_lock("nozzle-clean-test"):
        clean = state.nozzle_clean()
        if clean is None:
            raise HTTPException(
                status_code=400, detail="ノズルクリーニング位置が未記録です"
            )
        dispenser = state.machine().paste_dispenser
        messages: list[str] = []
        try:
            with klipper_errors_to_502():
                klipper = create_klipper(state, CLEAN_TIMEOUT)
                _require_all_axes_homed(klipper, "全軸ホーミング後に実行してください")
                stage = XYZStage(klipper.readonly)
                with build_applicator(klipper, stage, dispenser) as applicator:
                    clean_nozzle(klipper, stage, applicator, clean, log=messages.append)
                    applicator.retract()
        except ValueError as exc:
            # 可動域外などの不正な設定値。clean_nozzle は可動域の検証を
            # パージより前に通すので、この経路では装置は動いていない
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return NozzleCleanTestResult(message="\n".join(messages))
