"""Dataset 収集 1 回分の撮影・塗布結果を蓄積し、metadata.json を組み立てる."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import attrs

from pcbasm.config import PasteDispenser
from pcbasm.pasting.applicator import PasteApplicationResult
from pcbasm.pasting.dataset.metadata import (
    CAPTURE_ORDER,
    DatasetCapturedView,
    DatasetView,
    PasteDatasetBlank,
    PasteDatasetCamera,
    PasteDatasetConfig,
    PasteDatasetLabel,
    PasteDatasetLoading,
    PasteDatasetMachine,
    PasteDatasetMetadata,
    PasteDatasetNozzle,
    PasteDatasetPaste,
    PasteDatasetPlate,
)
from pcbasm.pasting.dataset.pending import (
    PENDING_KIND,
    PENDING_SCHEMA_VERSION,
    PasteDatasetPending,
    PasteDatasetPendingSample,
    finalize_pending,
    sample_key,
)
from pcbasm.pasting.dataset.plan import DotCell, DotGridPlan, DotTarget
from pcbasm.pasting.dataset.writer import PasteDatasetWriter
from pcbasm.utils import is_finite_number
from pcbasm.vision.calibration import CalibrationResult
from pcbasm.vision.crop import RectCrop


@attrs.frozen
class DatasetRunInfo:
    """Metadata に記録する収集 1 回分の環境情報（装置・ペースト・撮影設定）.

    銅板寸法・セル格子・量スイープ・blank 数・seed は :class:`DotGridPlan` の
    :class:`~pcbasm.pasting.dataset.plan.DotGridSpec` が唯一の出典なので持たない。

    Attributes:
        machine_id: 機体 ID（backend の自己申告 ID。そのまま機体名として記録する）
        paste_id: ペースト製品 ID
        paste_lot: 製造ロット（任意）
        paste_height_mm: 点塗布の塗布高さ [mm]
        height_plane_z_mm: 計測した銅板面の Z [mm]
        view_count: 周辺 view 数（中心 view を含まない）
        view_offset_mm: 周辺 view の移動距離 [mm]
        crop_size_px: 全 crop 共通のピクセル寸法
        loading: 塗布パス先頭のインタラクティブローディング実績
        started_at: 収集開始時刻（timezone 付き）
        dispenser: 収集時の ``[paste_dispenser]`` 設定
        calibration: 収集時のカメラ calibration
    """

    machine_id: str
    paste_id: str
    paste_lot: str | None
    paste_height_mm: float
    height_plane_z_mm: float
    view_count: int
    view_offset_mm: float
    crop_size_px: int
    loading: PasteDatasetLoading
    started_at: datetime
    dispenser: PasteDispenser
    calibration: CalibrationResult


def validate_dataset_run(
    *,
    paste_id: object,
    paste_height_mm: object,
) -> str | None:
    """Dataset 収集の開始条件（ペースト ID・塗布高さ）を装置を開く前に検証する."""
    if not isinstance(paste_id, str) or not paste_id.strip():
        return "paste_idは空にできません"
    if not is_finite_number(paste_height_mm) or paste_height_mm <= 0:
        return f"塗布高さは正の有限値が必要です: {paste_height_mm!r}"
    return None


class PasteDatasetRecorder:
    """撮影（pre/post）と塗布実績をセル単位で蓄積し、計量値から metadata を確定する."""

    def __init__(
        self,
        writer: PasteDatasetWriter,
        plan: DotGridPlan,
        views: Sequence[DatasetView],
    ) -> None:
        """撮影 view を検証して蓄積を始める.

        Raises:
            ValueError: view 番号が負か offset が有限値でない（呼び出し側の invariant）
        """
        for view in views:
            error = view.validate()
            if error is not None:
                raise ValueError(error)
        self._writer = writer
        self._plan = plan
        self._views = tuple(views)
        self._captured: dict[tuple[int, int], DatasetCapturedView] = {}
        self._executions: dict[str, PasteApplicationResult] = {}
        self._metadata: PasteDatasetMetadata | None = None

    @property
    def metadata(self) -> PasteDatasetMetadata | None:
        """:meth:`finalize` で確定した metadata（未確定なら ``None``）."""
        return self._metadata

    def record_pre(self, target: DotTarget, view: DatasetView, crop: RectCrop) -> None:
        """塗布前画像を書き、view 記述をセルと紐付けて保持する."""
        self._captured[(target.index, view.number)] = self._writer.write_capture(
            target.index, view, "pre", crop
        )

    def record_execution(self, cell: DotCell, result: PasteApplicationResult) -> None:
        """セルへの点塗布実績を記録する."""
        self._executions[sample_key(cell.index)] = result

    def record_post(
        self, target: DotTarget, view: DatasetView, crop: RectCrop
    ) -> str | None:
        """塗布後画像を書き、pre と crop 矩形が一致しなければ理由を返す."""
        post_view = self._writer.write_capture(target.index, view, "post", crop)
        pre_view = self._captured.get((target.index, view.number))
        if pre_view is None:
            return f"sample {target.index} のpre画像が記録されていません"
        if post_view.pixel_rect != pre_view.pixel_rect:
            return f"sample {target.index} のpre/post crop位置が一致しません"
        return None

    def build_pending(self, run: DatasetRunInfo) -> PasteDatasetPending:
        """蓄積した撮影・塗布実績から、計量質量だけが未確定の doc を組む."""
        plan = self._plan
        spec = plan.spec
        dispenser = run.dispenser
        calibration = run.calibration
        return PasteDatasetPending(
            kind=PENDING_KIND,
            schema_version=PENDING_SCHEMA_VERSION,
            created_at=run.started_at.isoformat(),
            # 機体名は machine_id をそのまま使う（machine.toml に表示名の設定は無い）
            machine=PasteDatasetMachine(machine_id=run.machine_id, name=run.machine_id),
            plate=PasteDatasetPlate(
                width_mm=spec.plate_width_mm,
                height_mm=spec.plate_height_mm,
                edge_margin_mm=spec.edge_margin_mm,
                height_plane_z_mm=run.height_plane_z_mm,
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
                max_dispense_rate_ul_s=dispenser.max_dispense_rate,
                dispense_accel_ul_s2=dispenser.dispense_accel,
                retract_amount_ul=dispenser.retract_amount,
                retract_rate_ul_s=dispenser.effective_retract_rate,
                paste_height_mm=run.paste_height_mm,
                prime_extra_delay_s=0.0,
                cell_size_mm=spec.cell_size_mm,
                cell_gap_mm=spec.cell_gap_mm,
                crop_size_mm=spec.crop_size_mm,
                crop_size_px=run.crop_size_px,
                volume_min_ul=spec.volume_min_ul,
                volume_max_ul=spec.volume_max_ul,
                volume_divisions=spec.volume_divisions,
                samples_per_volume=spec.samples_per_volume,
                blank_count=spec.blank_count,
                shuffle_seed=spec.shuffle_seed,
                view_count=run.view_count,
                view_offset_mm=run.view_offset_mm,
                capture_order=CAPTURE_ORDER,
            ),
            label=PasteDatasetLabel(kind="rotation_allocated"),
            loading=run.loading,
            samples=tuple(
                PasteDatasetPendingSample(
                    index=cell.index,
                    order=cell.order,
                    cell=cell.rect,
                    center=cell.center,
                    commanded_volume_ul=cell.commanded_volume_ul,
                    volume_index=cell.volume_index,
                    execution=self._executions[sample_key(cell.index)].summary,
                    views=self._views_of(cell),
                )
                for cell in plan.cells
            ),
            blanks=tuple(
                PasteDatasetBlank(
                    index=blank.index,
                    cell=blank.rect,
                    center=blank.center,
                    measured_volume_ul=0.0,
                    views=self._views_of(blank),
                )
                for blank in plan.blanks
            ),
        )

    def write_pending(self, run: DatasetRunInfo) -> Path:
        """計量質量を待つ前に、質量以外を確定させた doc を session へ残す.

        WebUI が落ちて質量を入力できなくても、この doc と計量値があれば
        :func:`~pcbasm.pasting.dataset.writer.finalize_incomplete` で
        本経路と同じ metadata に到達できる。
        """
        return self._writer.write_pending(self.build_pending(run))

    def finalize(self, *, measured_mass_mg: float, run: DatasetRunInfo) -> Path:
        """計量質量を回転数比でセルへ配分し、metadata を書いて session を確定する.

        Raises:
            ValueError: 計量質量が正でない、または回転数の配分ができない
        """
        metadata, error = finalize_pending(
            self.build_pending(run), measured_mass_mg=measured_mass_mg
        )
        if metadata is None:
            raise ValueError(error)
        path = self._writer.finalize(metadata)
        self._metadata = metadata
        return path

    def mark_incomplete(self) -> Path:
        """取得済みファイルを保持したまま incomplete session へ確定する."""
        return self._writer.mark_incomplete()

    def _views_of(self, target: DotTarget) -> tuple[DatasetCapturedView, ...]:
        return tuple(
            self._captured[(target.index, view.number)] for view in self._views
        )
