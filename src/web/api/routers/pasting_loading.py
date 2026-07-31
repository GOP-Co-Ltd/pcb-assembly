"""ペーストローディング 質量キャリブレーションの API（算出のみ。適用は settings API）."""

from __future__ import annotations

import attrs
from fastapi import APIRouter
from pydantic import BaseModel

from pcbasm.pasting import estimate_mass_flow
from web.api.dependencies import StateDep

router = APIRouter(prefix="/api")


class LoadingCalibrationResult(BaseModel):
    """質量キャリブレーションの算出結果（入力不足の値は null）."""

    volume_ul: float | None
    rotations_per_ul: float | None
    max_dispense_rate: float | None
    dispense_accel: float | None


@router.get("/pasting/loading/calibration")
def get_loading_calibration(
    state: StateDep,
    mass_mg: float = 0.0,
    rotations: float = 0.0,
    rate: float = 0.0,
    accel: float = 0.0,
) -> LoadingCalibrationResult:
    """計測質量・回転数・速度・加速度から塗布キャリブレーション値を算出する.

    密度はサーバ側のマシン設定 ``solder_paste_density`` を真実とする。
    非正入力は該当値を ``None`` で返す（エラーにしない）。算出は
    :func:`pcbasm.pasting.estimate_mass_flow` へ委譲する（丸め済み）。
    """
    density = state.machine().paste_dispenser.solder_paste_density
    estimate = estimate_mass_flow(
        mass_mg=mass_mg,
        rotations=rotations,
        rate=rate,
        accel=accel,
        density_mg_per_ul=density,
    )
    return LoadingCalibrationResult(**attrs.asdict(estimate))
