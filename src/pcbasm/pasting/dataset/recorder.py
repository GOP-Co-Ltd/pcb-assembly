"""Dataset 収集 1 回分の撮影・塗布結果を蓄積し、metadata.json を組み立てる."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import attrs

from pcbasm.config import PasteDispenser
from pcbasm.pasting.applicator import PasteApplicationResult
from pcbasm.pasting.dataset.metadata import (
    METADATA_KIND,
    METADATA_SCHEMA_VERSION,
    DatasetCapturedView,
    DatasetPolygon,
    DatasetView,
    PasteDatasetBoard,
    PasteDatasetCamera,
    PasteDatasetConfig,
    PasteDatasetMachine,
    PasteDatasetMetadata,
    PasteDatasetNozzle,
    PasteDatasetPad,
    PasteDatasetPaste,
    PasteDatasetPurge,
    PasteDatasetTotal,
    allocate_volume_by_rotations,
    validate_view,
)
from pcbasm.pasting.dataset.writer import PasteDatasetWriter
from pcbasm.pasting.workflow import DatasetTargets
from pcbasm.pcb import Pad
from pcbasm.utils import is_finite_number
from pcbasm.vision.calibration import CalibrationResult
from pcbasm.vision.crop import PolygonCrop, validate_crop_margins


@attrs.frozen
class DatasetRunInfo:
    """Metadata に記録する収集 1 回分の環境情報（装置・基板・ペースト・設定）."""

    machine_id: str
    machine_name: str | None
    pcb_filename: str
    source_pcb: str
    paste_id: str
    paste_lot: str | None
    crop_margin_mm: float
    mask_margin_mm: float
    started_at: datetime
    dispenser: PasteDispenser
    calibration: CalibrationResult


def validate_dataset_run(
    *,
    initial_purge_ul: object,
    paste_id: object,
    crop_margin_mm: object,
    mask_margin_mm: object,
) -> str | None:
    """Dataset 収集の開始条件（purge 量・ペースト ID・余白）を装置を開く前に検証する."""
    if not is_finite_number(initial_purge_ul) or initial_purge_ul <= 0:
        return "dataset収集にはinitial_purge_ulを正の値で設定してください"
    if not isinstance(paste_id, str) or not paste_id.strip():
        return "paste_idは空にできません"
    if not is_finite_number(crop_margin_mm):
        return f"crop_margin_mmは0以上の有限値が必要です: {crop_margin_mm!r}"
    if not is_finite_number(mask_margin_mm):
        return f"mask_margin_mmは0以上の有限値が必要です: {mask_margin_mm!r}"
    return validate_crop_margins(float(crop_margin_mm), float(mask_margin_mm))


class PasteDatasetRecorder:
    """撮影（pre/post）と塗布実績を pad 単位で蓄積し、計量値から metadata を確定する."""

    def __init__(
        self,
        writer: PasteDatasetWriter,
        targets: DatasetTargets,
        views: Sequence[DatasetView],
    ) -> None:
        """撮影 view を検証して蓄積を始める.

        Raises:
            ValueError: view 番号が負か offset が有限値でない（呼び出し側の invariant）
        """
        for view in views:
            error = validate_view(view)
            if error is not None:
                raise ValueError(error)
        self._writer = writer
        self._targets = targets
        self._views = tuple(views)
        self._captured: dict[tuple[str, int], DatasetCapturedView] = {}
        self._executions: dict[str, PasteApplicationResult] = {}
        self._metadata: PasteDatasetMetadata | None = None

    @property
    def metadata(self) -> PasteDatasetMetadata | None:
        """:meth:`finalize` で確定した metadata（未確定なら ``None``）."""
        return self._metadata

    def record_pre(
        self, index: int, pad: Pad, view: DatasetView, crop: PolygonCrop
    ) -> None:
        """塗布前画像を書き、view 記述を pad と紐付けて保持する."""
        pad_id = self._targets.hierarchy.pad_id_for_pad(pad)
        self._captured[(pad_id, view.number)] = self._writer.write_capture(
            index, view, "pre", crop
        )

    def record_execution(self, pad_id: str, result: PasteApplicationResult) -> None:
        """Pad（または purge）の塗布実績を記録する."""
        self._executions[pad_id] = result

    def record_post(
        self, index: int, pad: Pad, view: DatasetView, crop: PolygonCrop
    ) -> str | None:
        """塗布後画像を書き、pre と crop 矩形が一致しなければ理由を返す."""
        pad_id = self._targets.hierarchy.pad_id_for_pad(pad)
        post_view = self._writer.write_capture(index, view, "post", crop)
        pre_view = self._captured.get((pad_id, view.number))
        if pre_view is None:
            return f"{pad_id} のpre画像が記録されていません"
        if post_view.pixel_rect != pre_view.pixel_rect:
            return f"{pad_id} のpre/post crop位置が一致しません"
        return None

    def finalize(self, *, measured_mass_mg: float, run: DatasetRunInfo) -> Path:
        """計量質量を回転数比で pad へ配分し、metadata を書いて session を確定する."""
        targets = self._targets
        dispenser = run.dispenser
        measured_volume_ul = measured_mass_mg / dispenser.solder_paste_density
        rotations = {
            key: execution.summary.rotations
            for key, execution in self._executions.items()
        }
        allocated = allocate_volume_by_rotations(measured_volume_ul, rotations)
        pads = tuple(
            PasteDatasetPad(
                index=index,
                pad_id=(pad_id := targets.hierarchy.pad_id_for_pad(pad)),
                source_pad_id=f"{pad.designator}.{pad.pad_number}",
                polygon=DatasetPolygon.from_polygon(pad.polygon),
                resolved=targets.params_for(pad),
                execution=self._executions[pad_id].summary,
                measured_volume_ul=allocated[pad_id],
                views=tuple(
                    self._captured[(pad_id, view.number)] for view in self._views
                ),
            )
            for index, pad in enumerate(targets.sample_pads, start=1)
        )
        calibration = run.calibration
        purge_pad = targets.purge_pad
        metadata = PasteDatasetMetadata(
            kind=METADATA_KIND,
            schema_version=METADATA_SCHEMA_VERSION,
            created_at=run.started_at.isoformat(),
            machine=PasteDatasetMachine(
                machine_id=run.machine_id, name=run.machine_name
            ),
            board=PasteDatasetBoard(
                filename=run.pcb_filename,
                source_pcb=run.source_pcb,
                signature=targets.hierarchy.signature(),
            ),
            paste=PasteDatasetPaste(
                paste_id=run.paste_id,
                lot=run.paste_lot,
                density_mg_per_ul=dispenser.solder_paste_density,
            ),
            camera=PasteDatasetCamera(
                pixel_per_mm=calibration.pixel_per_mm,
                resolution=calibration.resolution,
                calibrated_at=calibration.calibrated_at.isoformat(),
                z_position_mm=calibration.z_position,
            ),
            nozzle=PasteDatasetNozzle(diameter_mm=dispenser.nozzle_diameter),
            config=PasteDatasetConfig(
                rotations_per_ul=dispenser.rotations_per_ul,
                max_fill_speed_mm_s=dispenser.max_fill_speed,
                max_dispense_rate_ul_s=dispenser.max_dispense_rate,
                dispense_accel_ul_s2=dispenser.dispense_accel,
                retract_amount_ul=dispenser.retract_amount,
                retract_rate_ul_s=dispenser.effective_retract_rate,
                initial_purge_ul=dispenser.initial_purge_ul,
                crop_margin_mm=run.crop_margin_mm,
                mask_margin_mm=run.mask_margin_mm,
            ),
            total=PasteDatasetTotal(
                measured_mass_mg=measured_mass_mg,
                measured_volume_ul=measured_volume_ul,
                rotations=sum(rotations.values()),
            ),
            purge=PasteDatasetPurge(
                pad_id=targets.purge_pad_id,
                source_pad_id=f"{purge_pad.designator}.{purge_pad.pad_number}",
                execution=self._executions[targets.purge_pad_id].summary,
                measured_volume_ul=allocated[targets.purge_pad_id],
            ),
            pads=pads,
        )
        path = self._writer.finalize(metadata)
        self._metadata = metadata
        return path

    def mark_incomplete(self) -> Path:
        """取得済みファイルを保持したまま incomplete session へ確定する."""
        return self._writer.mark_incomplete()
