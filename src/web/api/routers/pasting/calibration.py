"""吐出量・塗布量の校正を補助する読み取り専用の API（装置を動かさないので操作権は要求しない）.

- 質量からの吐出キャリブレーション値の算出（適用は ``PUT /api/machine_config``）
- 保存済み塗布量校正の一覧（収集ジョブの ``volume_calibration`` 入力の ``<select>`` 用）
- データセット収集のレイアウト preview

表示文字列はここで組み立て、JS では連結しない。入力が不正・配置不能・ファイルが壊れているときも 200 で返し、理由を body に載せる。
"""

from __future__ import annotations

from pathlib import Path

import attrs
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from pcbasm.pasting.dispense_calibration.mass import MassFlowEstimate
from pcbasm.pasting.paste_volume.calibration import list_calibrations, load_calibration
from pcbasm.pasting.paste_volume.dataset.plan import (
    grid_spec_from_params,
    preview_dot_grid,
)
from web.api.deps import EnvDep, MachineConfigDep
from web.contract.models import Point2dInfo, RectInfo

router = APIRouter(prefix="/api/pasting/calibration")


class MassFlowResult(BaseModel):
    """質量キャリブレーションの算出結果（入力不足の値は null）."""

    volume_ul: float | None
    rotations_per_ul: float | None
    max_dispense_rate: float | None
    dispense_accel: float | None


@router.get("/mass-flow")
def get_mass_flow(
    machine_config: MachineConfigDep,
    mass_mg: float = 0.0,
    rotations: float = 0.0,
    rate: float = 0.0,
    accel: float = 0.0,
) -> MassFlowResult:
    """計測質量・回転数・速度・加速度から塗布キャリブレーション値を算出する.

    密度はサーバ側のマシン設定 ``solder_paste_density`` の値を使う。
    非正・非有限入力や表現不能な算出値は ``None`` で返す（エラーにしない）。算出は
    :meth:`pcbasm.pasting.dispense_calibration.mass.MassFlowEstimate.estimate` へ委譲する（丸め済み）。
    """
    density = machine_config.machine().paste_dispenser.solder_paste_density
    estimate = MassFlowEstimate.estimate(
        mass_mg=mass_mg,
        rotations=rotations,
        rate=rate,
        accel=accel,
        density_mg_per_ul=density,
    )
    return MassFlowResult(**attrs.asdict(estimate))


class CalibrationSummary(BaseModel):
    """一覧に出す校正 1 件.

    ``option_label`` と ``details`` はそのまま表示できる形で返す。JS 側で連結や
    フォールバックを組まないための項目で、壊れたファイルでも空にはならない。
    """

    name: str
    option_label: str
    details: str
    label: str | None
    created_at: str | None
    conditions: str | None
    diagnostics: str | None
    error: str | None


class CalibrationsResponse(BaseModel):
    """保存済み校正の一覧."""

    calibrations: list[CalibrationSummary]


@router.get("/paste-volume")
def list_paste_volume_calibrations(env: EnvDep) -> CalibrationsResponse:
    """保存済み校正をファイル名順に返す（壊れたファイルも 1 件として載せる）.

    1 つ壊れているせいで一覧そのものが消えると、どれが壊れているのか WebUI から分からなくなる。
    """
    return CalibrationsResponse(
        calibrations=[
            _summary(path)
            for path in list_calibrations(env.paste_volume_calibration_dir)
        ]
    )


def _summary(path: Path) -> CalibrationSummary:
    """校正ファイル 1 件を表示用の要約へ変換する."""
    calibration, error = load_calibration(path)
    if calibration is None:
        return CalibrationSummary(
            name=path.name,
            option_label=f"{path.name}（読めません）",
            details=error or "",
            label=None,
            created_at=None,
            conditions=None,
            diagnostics=None,
            error=error,
        )
    conditions = calibration.conditions
    diagnostics = calibration.diagnostics
    conditions_text = (
        f"{conditions.paste_id} / ノズル {conditions.nozzle_diameter_mm} mm / "
        f"塗布高さ {conditions.paste_height_mm} mm / "
        f"{conditions.pixel_per_mm:.3f} px/mm"
    )
    diagnostics_text = (
        f"塗布 {calibration.source.sample_count} 点 / "
        f"総体積誤差 {diagnostics.total_relative_error * 100:+.2f}% / "
        f"点ごと残差std {diagnostics.residual_relative_std * 100:.1f}%"
    )
    return CalibrationSummary(
        name=path.name,
        option_label=calibration.label or path.name,
        details=f"{conditions_text} / {diagnostics_text}",
        label=calibration.label,
        created_at=calibration.created_at,
        conditions=conditions_text,
        diagnostics=diagnostics_text,
        error=None,
    )


class DatasetLayoutRequest(BaseModel):
    """レイアウト設定（フィールド名は収集ジョブの ``ParamSpec`` と一致させる）.

    WebUI はジョブフォームの入力値を全部そのまま送る。レイアウトに関係しない項目は無視する。
    ``shuffle_seed`` は解決せずに渡すので、``0`` の preview は決定論的な配置になる（実行時は別シードで再配置される）。
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    plate_width: float
    plate_height: float
    edge_margin: float
    cell_size: float
    cell_gap: float
    crop_size: float
    volume_min: float
    volume_max: float
    volume_divisions: int
    samples_per_volume: int
    blank_count: int
    view_count: int
    view_offset: float
    shuffle_seed: int


class DotGridSpecInfo(BaseModel):
    plate_width_mm: float
    plate_height_mm: float
    edge_margin_mm: float
    cell_size_mm: float
    cell_gap_mm: float
    crop_size_mm: float
    volume_min_ul: float
    volume_max_ul: float
    volume_divisions: int
    samples_per_volume: int
    blank_count: int
    shuffle_seed: int


class DotCellInfo(BaseModel):
    index: int
    rect: RectInfo
    center: Point2dInfo
    commanded_volume_ul: float
    volume_index: int
    order: int


class DotBlankInfo(BaseModel):
    index: int
    rect: RectInfo
    center: Point2dInfo


class DatasetLayoutResponse(BaseModel):
    """銅板のセル配置・使用セル・撮影枚数（配置不能なら ``error`` に理由を載せ、図は消さない）."""

    spec: DotGridSpecInfo
    plate: RectInfo
    usable_area: RectInfo | None
    grid: list[RectInfo]
    cells: list[DotCellInfo]
    blanks: list[DotBlankInfo]
    capacity: int
    sample_count: int
    target_count: int
    volumes_ul: list[float]
    views_per_cell: int
    image_count: int
    error: str | None


@router.post("/dataset-layout")
def calculate_dataset_layout(body: DatasetLayoutRequest) -> DatasetLayoutResponse:
    """銅板のセル配置・使用セル・撮影枚数を返す（POST だが読み取り専用計算）.

    セル配置・容量判定・量割り当て・派生カウント（総点数・撮影枚数）は
    :func:`pcbasm.pasting.paste_volume.dataset.plan.preview_dot_grid` が唯一の出典。
    """
    preview = preview_dot_grid(
        grid_spec_from_params(body.model_dump()),
        view_count=body.view_count,
        view_offset_mm=body.view_offset,
    )
    return DatasetLayoutResponse.model_validate(preview, from_attributes=True)
