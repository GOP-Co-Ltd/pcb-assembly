"""保存済み塗布量校正の一覧 API（装置を動かさない読み取り専用）.

収集ジョブの ``volume_calibration`` 入力を ``<select>`` に差し替え、選択中の条件と
診断を出すためだけに使う。表示文字列はここで組み立て、JS では連結しない。

壊れたファイルがあっても 200 で返し、理由は各要素の ``error`` に載せる（1 つ壊れて
いるせいで一覧そのものが消えると、どれが壊れているのか WebUI から分からなくなる）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from pcbasm.pasting.paste_volume.calibration import list_calibrations, load_calibration
from web.api.dependencies import SettingsDep

router = APIRouter(prefix="/api/pasting/paste-volume", tags=["pasting"])


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


@router.get("/calibrations")
def list_paste_volume_calibrations(settings: SettingsDep) -> CalibrationsResponse:
    """保存済み校正をファイル名順に返す（壊れたファイルも 1 件として載せる）."""
    return CalibrationsResponse(
        calibrations=[
            _summary(path)
            for path in list_calibrations(settings.paste_volume_calibration_dir)
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
