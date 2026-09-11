"""1 つの収集 session を計測し、直径 → 体積の校正を組み立てる.

`reader.DatasetSession` が読んだ metadata の各セルについて全 view を
:func:`~pcbasm.pasting.paste_volume.detect.measure_dot` にかけ、中央値へ集約してから
切片 0 固定の 3 次モデルへ最小二乗で当てる。

教師体積 `measured_volume_ul` は総質量を指令回転数比で配分したラベル
（`label.kind = "rotation_allocated"`）で、点ごとの真値ではない。よって当てはまりの
主基準は session 総体積の相対誤差とし、点ごとの誤差は参考値として診断へ載せる。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import attrs
import cv2
import numpy as np

from pcbasm.pasting.dataset.metadata import (
    DatasetCapturedView,
    PasteDatasetBlank,
)
from pcbasm.pasting.dataset.reader import DatasetSession
from pcbasm.pasting.dataset.writer import METADATA_FILENAME
from pcbasm.pasting.paste_volume.aggregate import DotDiameter, aggregate_views
from pcbasm.pasting.paste_volume.calibration import (
    CALIBRATION_KIND,
    CALIBRATION_SCHEMA_VERSION,
    CalibrationConditions,
    CalibrationDiagnostics,
    CalibrationSource,
    PasteVolumeCalibration,
    default_calibration_label,
)
from pcbasm.pasting.paste_volume.detect import (
    DotDetectionSpec,
    DotMeasurement,
    measure_dot,
)
from pcbasm.pasting.paste_volume.model import (
    CubicVolumeModel,
    fit_cubic_through_origin,
)
from pcbasm.vision.image import ImageArray


@attrs.frozen
class CellMeasurement:
    """1 セルの計測結果（フィットの材料であり、診断と可視化の材料でもある）.

    Attributes:
        index: セル番号（metadata の `index`）
        blank: blank セルなら True（教師体積 0、フィットには寄与しない）
        measured_volume_ul: 配分された教師体積 [µL]
        diameter: 全 view を中央値へ集約した直径
    """

    index: int
    blank: bool
    measured_volume_ul: float
    diameter: DotDiameter


@attrs.frozen
class CalibrationFit:
    """校正ファイルと、その根拠になった計測列.

    `predicted_volumes_ul` は `cells` と同順・同長で、散布図と誤差表を作るのに使う。
    """

    calibration: PasteVolumeCalibration
    cells: tuple[CellMeasurement, ...]
    predicted_volumes_ul: tuple[float, ...]


def measure_session(
    session: DatasetSession, *, spec: DotDetectionSpec = DotDetectionSpec()
) -> tuple[tuple[CellMeasurement, ...] | None, str | None]:
    """Session の全セルを計測する（画像や設定の構造的不正は理由を返す）.

    「はんだが写っていない」は正常系なので失敗にしない。

    直径 0 として返し、blank の誤検出数と検出失敗数は診断で数える。
    """
    error = spec.validate()
    if error is not None:
        return None, error
    pixel_per_mm = session.pixel_per_mm

    cells: list[CellMeasurement] = []
    for cell in session.cells():
        measurements, error = _measure_views(
            session, cell.views, pixel_per_mm=pixel_per_mm, spec=spec
        )
        if measurements is None:
            return None, f"セル{cell.index}: {error}"
        cells.append(
            CellMeasurement(
                index=cell.index,
                blank=isinstance(cell, PasteDatasetBlank),
                measured_volume_ul=cell.measured_volume_ul,
                diameter=aggregate_views(measurements),
            )
        )
    if not cells:
        return None, f"計測できるセルがありません: {session.label}"
    return tuple(cells), None


def fit_session(
    session: DatasetSession,
    *,
    spec: DotDetectionSpec = DotDetectionSpec(),
    label: str | None = None,
    created_at: datetime | None = None,
    require_blank_zero: bool = True,
) -> tuple[CalibrationFit | None, str | None]:
    """Session を計測してフィットし、校正ファイルへ組み立てる.

    Args:
        session: 材料にする完成 session
        spec: 検出ハイパラ（校正と不可分なのでそのまま校正へ埋め込む）
        label: 校正の表示名（省略時は条件から組み立てる）
        created_at: 生成時刻（省略時は現在時刻）
        require_blank_zero: blank の直径が 0 にならなければ失敗させる

    Returns:
        `(フィット結果, None)` または `(None, 理由)`
    """
    cells, error = measure_session(session, spec=spec)
    if cells is None:
        return None, error

    blank_false_positives = sum(
        1 for cell in cells if cell.blank and cell.diameter.diameter_mm > 0.0
    )
    if require_blank_zero and blank_false_positives:
        return None, (
            f"blankセルで直径が0になりませんでした: {blank_false_positives}点"
            "（min_contrastを上げるか、素材を確認してください）"
        )

    dispensed = [cell for cell in cells if not cell.blank]
    model, error = fit_cubic_through_origin(
        [cell.diameter.diameter_mm for cell in dispensed],
        [cell.measured_volume_ul for cell in dispensed],
    )
    if model is None:
        return None, error

    predicted = tuple(model.volume_ul(cell.diameter.diameter_mm) for cell in cells)
    metadata = session.metadata
    moment = created_at or datetime.now(UTC)
    conditions = CalibrationConditions(
        paste_id=metadata.paste.paste_id,
        paste_lot=metadata.paste.lot,
        density_mg_per_ul=metadata.paste.density_mg_per_ul,
        nozzle_diameter_mm=metadata.nozzle.diameter_mm,
        paste_height_mm=metadata.config.paste_height_mm,
        machine_id=metadata.machine.machine_id,
        pixel_per_mm=metadata.camera.pixel_per_mm,
        crop_size_px=metadata.config.crop_size_px,
        crop_size_mm=metadata.config.crop_size_mm,
    )
    calibration = PasteVolumeCalibration(
        kind=CALIBRATION_KIND,
        schema_version=CALIBRATION_SCHEMA_VERSION,
        created_at=moment.isoformat(),
        label=label or default_calibration_label(conditions, moment),
        conditions=conditions,
        detection=spec,
        model=model,
        source=CalibrationSource(
            session=session.label,
            label_kind=metadata.label.kind,
            sample_count=len(metadata.samples),
            blank_count=len(metadata.blanks),
            measured_volume_ul=metadata.total.measured_volume_ul,
            metadata_sha256=_metadata_digest(session.root),
        ),
        diagnostics=_diagnostics(
            cells,
            predicted,
            model=model,
            blank_false_positives=blank_false_positives,
        ),
    )
    error = calibration.validate()
    if error is not None:
        return None, error
    return CalibrationFit(
        calibration=calibration, cells=cells, predicted_volumes_ul=predicted
    ), None


def _measure_views(
    session: DatasetSession,
    views: Sequence[DatasetCapturedView],
    *,
    pixel_per_mm: float,
    spec: DotDetectionSpec,
) -> tuple[tuple[DotMeasurement, ...] | None, str | None]:
    """1 セルの全 view を計測する."""
    if not views:
        return None, "viewがありません"
    measurements: list[DotMeasurement] = []
    for view in views:
        pre, error = _load_view_image(session, view.pre)
        if pre is None:
            return None, error
        post, error = _load_view_image(session, view.post)
        if post is None:
            return None, error
        measurement, error = measure_dot(
            pre, post, pixel_per_mm=pixel_per_mm, spec=spec
        )
        if measurement is None:
            return None, f"view{view.number}: {error}"
        measurements.append(measurement)
    return tuple(measurements), None


def _load_view_image(
    session: DatasetSession, relative: str
) -> tuple[ImageArray | None, str | None]:
    """Metadata の相対 path から画像を読む（解決も読み込みも失敗は理由を返す）."""
    path, error = session.image_path(relative)
    if path is None:
        return None, error
    image = cv2.imread(str(path))
    if image is None:
        return None, f"画像を読めません: {relative}"
    return image, None


def _diagnostics(
    cells: Sequence[CellMeasurement],
    predicted_volumes_ul: Sequence[float],
    *,
    model: CubicVolumeModel,
    blank_false_positives: int,
) -> CalibrationDiagnostics:
    """当てはまりと検出の健全性を集計する.

    点ごとの相対誤差は教師体積が正のセルだけで採る。

    blank は分母が 0 になるうえ、ラベルの性質上そもそも点ごとの真値を持たない。
    """
    relative_errors = [
        (predicted - cell.measured_volume_ul) / cell.measured_volume_ul
        for cell, predicted in zip(cells, predicted_volumes_ul, strict=True)
        if not cell.blank and cell.measured_volume_ul > 0.0
    ]
    errors = np.array(relative_errors, dtype=np.float64)
    measured_total = sum(cell.measured_volume_ul for cell in cells)
    predicted_total = sum(predicted_volumes_ul)
    return CalibrationDiagnostics(
        blank_false_positive_count=blank_false_positives,
        detection_failure_count=sum(
            1
            for cell in cells
            if not cell.blank and cell.diameter.detected_view_count == 0
        ),
        monotonic_in_range=model.is_monotonic_in_range(),
        residual_relative_std=float(errors.std()) if errors.size else 0.0,
        point_relative_mae=float(np.abs(errors).mean()) if errors.size else 0.0,
        point_relative_max=float(np.abs(errors).max()) if errors.size else 0.0,
        total_relative_error=(
            (predicted_total - measured_total) / measured_total
            if measured_total > 0.0
            else 0.0
        ),
    )


def _metadata_digest(root: Path) -> str:
    """材料にした ``metadata.json`` の SHA-256（後から材料を突き合わせるため）."""
    return hashlib.sha256((root / METADATA_FILENAME).read_bytes()).hexdigest()


__all__ = [
    "CalibrationFit",
    "CellMeasurement",
    "fit_session",
    "measure_session",
]
