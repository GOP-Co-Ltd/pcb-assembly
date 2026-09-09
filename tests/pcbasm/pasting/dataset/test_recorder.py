"""Dataset recorder（撮影・塗布実績の蓄積 → metadata.json 組立）の公開契約.

``plan_dot_grid`` が返す :class:`DotGridPlan` を対象に、
record_pre → record_execution → record_post → finalize が schema v2 と同じキー集合
（``data/testing/schemas/paste_dataset_metadata_v2.json``）を書くことを検証する。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from pcbasm.config import PasteDispenser as PasteDispenserConfig, Toolhead
from pcbasm.pasting.applicator import DispenseExecution, PasteApplicationResult
from pcbasm.pasting.dataset.metadata import DatasetView, parse_metadata
from pcbasm.pasting.dataset.plan import DotGridPlan, DotGridSpec, plan_dot_grid
from pcbasm.pasting.dataset.recorder import (
    DatasetRunInfo,
    PasteDatasetRecorder,
    validate_dataset_run,
)
from pcbasm.pasting.dataset.writer import PasteDatasetWriter
from pcbasm.vision.calibration import CalibrationResult
from pcbasm.vision.crop import RectCrop
from tests.helpers import TESTING_DATA_DIR

METADATA_V2 = TESTING_DATA_DIR / "schemas" / "paste_dataset_metadata_v2.json"
STARTED_AT = datetime(2026, 9, 8, 14, 30, 52, 123456, tzinfo=UTC)
DENSITY = 3.78
PASTE_HEIGHT_MM = 0.2
CROP_SIZE_PX = 9
HEIGHT_PLANE_Z_MM = 1.62
VIEW = DatasetView(number=0)
PURGE_ROTATIONS = 2.0
SAMPLE_ROTATIONS = 3.0
MEASURED_MASS_MG = 1.89


def _spec() -> DotGridSpec:
    return DotGridSpec(
        plate_width_mm=20.0,
        plate_height_mm=20.0,
        edge_margin_mm=2.0,
        cell_size_mm=2.0,
        cell_gap_mm=1.0,
        purge_cell_size_mm=2.0,
        volume_min_ul=0.05,
        volume_max_ul=0.2,
        crop_size_mm=2.0,
        volume_divisions=2,
        samples_per_volume=1,
        blank_count=2,
        shuffle_seed=20260908,
    )


def _crop(
    pixel_rect: tuple[int, int, int, int] = (
        10,
        20,
        10 + CROP_SIZE_PX,
        20 + CROP_SIZE_PX,
    ),
) -> RectCrop:
    image = np.full((CROP_SIZE_PX, CROP_SIZE_PX, 3), 127, dtype=np.uint8)
    return RectCrop(image=image, pixel_rect=pixel_rect)


def _execution(rotations: float) -> PasteApplicationResult:
    return PasteApplicationResult(
        (
            DispenseExecution(
                applied_mode="dot",
                path_length_mm=0.0,
                commanded_volume_ul=rotations / 20.0,
                prime_extra_volume_ul=0.0,
                effective_rate_ul_s=1.0,
                rotations=rotations,
                fill_speed=None,
            ),
        )
    )


def _dispenser() -> PasteDispenserConfig:
    return PasteDispenserConfig(
        rotations_per_ul=20.0,
        nozzle_diameter=0.34,
        max_fill_speed=2.0,
        max_dispense_rate=5.0,
        dispense_accel=10.0,
        retract_amount=10.0,
        retract_rate=10.0,
        retract_accel_factor=2.0,
        toolhead=Toolhead(x=0.0, y=0.0),
        paste_height=0.5,
        prime_extra_delay=0.8,
        ul_per_mm2=0.05,
        solder_paste_density=DENSITY,
        initial_purge_ul=0.5,
    )


def _run_info() -> DatasetRunInfo:
    return DatasetRunInfo(
        machine_id="machine-1",
        machine_name="Machine 1",
        paste_id="paste-1",
        paste_lot=None,
        paste_height_mm=PASTE_HEIGHT_MM,
        height_plane_z_mm=HEIGHT_PLANE_Z_MM,
        view_count=0,
        view_offset_mm=1.0,
        crop_size_px=CROP_SIZE_PX,
        started_at=STARTED_AT,
        dispenser=_dispenser(),
        calibration=CalibrationResult(
            pixel_per_mm=120.5,
            square_size_mm=1.0,
            mean_distance_px=120.5,
            std_distance_px=0.1,
            resolution=(1280, 720),
            crop_size=(600, 600),
            calibrated_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC),
            z_position=12.0,
        ),
    )


def _key_paths(value: object, prefix: str = "") -> set[str]:
    """JSON のキー階層を path 集合にする（配列は要素をまとめて ``[]``）."""
    if isinstance(value, dict):
        return {
            path
            for key, child in value.items()
            for path in {f"{prefix}{key}"} | _key_paths(child, f"{prefix}{key}.")
        }
    if isinstance(value, list):
        return set().union(*(_key_paths(item, f"{prefix}[].") for item in value))
    return set()


@pytest.fixture(scope="module")
def plan() -> DotGridPlan:
    planned, error = plan_dot_grid(_spec())

    assert error is None
    assert planned is not None
    return planned


def _recorder(tmp_path: Path, plan: DotGridPlan) -> PasteDatasetRecorder:
    return PasteDatasetRecorder(
        PasteDatasetWriter.open(
            tmp_path,
            plate_name="plate-20x20",
            crop_size_px=CROP_SIZE_PX,
            started_at=STARTED_AT,
        ),
        plan,
        (VIEW,),
    )


def _record_all(recorder: PasteDatasetRecorder, plan: DotGridPlan) -> None:
    """3 パス（全点 pre → パージ → 全点塗布 → 全点 post）で 1 セッションぶんを記録する."""
    for target in plan.targets:
        recorder.record_pre(target, VIEW, _crop())
    recorder.record_purge_execution(_execution(PURGE_ROTATIONS))
    for cell in plan.cells:
        recorder.record_execution(cell, _execution(SAMPLE_ROTATIONS))
    for target in plan.targets:
        assert recorder.record_post(target, VIEW, _crop()) is None


class TestValidateDatasetRun:
    """Dataset 収集の開始条件（purge 量・ペースト ID・塗布高さ）を装置前に検証する."""

    def test_accepts_positive_purge_paste_id_and_paste_height(self):
        assert (
            validate_dataset_run(
                initial_purge_ul=0.5, paste_id="paste-1", paste_height_mm=0.2
            )
            is None
        )

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"initial_purge_ul": 0.0}, "initial_purge_ul"),
            ({"initial_purge_ul": -0.1}, "initial_purge_ul"),
            ({"paste_id": ""}, "paste_id"),
            ({"paste_id": "   "}, "paste_id"),
            ({"paste_id": None}, "paste_id"),
            ({"paste_height_mm": 0.0}, "塗布高さ"),
            ({"paste_height_mm": -0.1}, "塗布高さ"),
            ({"paste_height_mm": "auto"}, "塗布高さ"),
            ({"paste_height_mm": float("nan")}, "塗布高さ"),
        ],
    )
    def test_reports_first_invalid_condition(
        self, overrides: dict[str, object], expected: str
    ):
        params: dict[str, object] = {
            "initial_purge_ul": 0.5,
            "paste_id": "paste-1",
            "paste_height_mm": 0.2,
        }
        params.update(overrides)

        error = validate_dataset_run(**params)  # type: ignore[arg-type]

        assert error is not None
        assert expected in error


class TestPasteDatasetRecorder:
    """record_pre / record_execution / record_post → finalize の metadata 組立."""

    def test_rejects_invalid_view_before_recording(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        writer = PasteDatasetWriter.open(
            tmp_path,
            plate_name="plate-20x20",
            crop_size_px=CROP_SIZE_PX,
            started_at=STARTED_AT,
        )

        with pytest.raises(ValueError, match="view number"):
            PasteDatasetRecorder(writer, plan, (DatasetView(number=-1),))

    def test_finalize_writes_metadata_with_schema_v2_key_set(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        session = recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        payload = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
        expected = json.loads(METADATA_V2.read_text(encoding="utf-8"))
        assert _key_paths(payload) == _key_paths(expected)
        parsed, error = parse_metadata(payload)
        assert error is None
        assert parsed is not None
        assert recorder.metadata == parsed

    def test_finalize_does_not_write_any_mask(self, tmp_path: Path, plan: DotGridPlan):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        session = recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        assert {path.name for path in session.iterdir()} == {
            "pre",
            "post",
            "metadata.json",
        }

    def test_finalize_allocates_measured_volume_by_rotations(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        total_volume = MEASURED_MASS_MG / DENSITY
        sample_count = len(plan.cells)
        total_rotations = PURGE_ROTATIONS + SAMPLE_ROTATIONS * sample_count
        assert metadata.total.measured_mass_mg == MEASURED_MASS_MG
        assert metadata.total.measured_volume_ul == pytest.approx(total_volume)
        assert metadata.total.rotations == pytest.approx(total_rotations)
        assert metadata.purge.measured_volume_ul == pytest.approx(
            total_volume * PURGE_ROTATIONS / total_rotations
        )
        assert len(metadata.samples) == sample_count
        assert sum(
            sample.measured_volume_ul for sample in metadata.samples
        ) + metadata.purge.measured_volume_ul == pytest.approx(total_volume)

    def test_finalize_copies_cell_geometry_and_commanded_volume_from_the_plan(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        cells = plan.cells
        assert [sample.index for sample in metadata.samples] == [
            cell.index for cell in cells
        ]
        assert [sample.cell for sample in metadata.samples] == [
            cell.rect for cell in cells
        ]
        assert [sample.center for sample in metadata.samples] == [
            cell.center for cell in cells
        ]
        assert [sample.commanded_volume_ul for sample in metadata.samples] == [
            cell.commanded_volume_ul for cell in cells
        ]
        assert [sample.volume_index for sample in metadata.samples] == [
            cell.volume_index for cell in cells
        ]
        assert metadata.purge.cell == plan.purge_cell
        assert metadata.purge.center == plan.purge_center

    def test_finalize_records_plate_and_dot_grid_config(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        spec = plan.spec
        assert metadata.created_at == STARTED_AT.isoformat()
        assert metadata.machine.machine_id == "machine-1"
        assert metadata.paste.paste_id == "paste-1"
        assert metadata.paste.lot is None
        assert metadata.paste.density_mg_per_ul == DENSITY
        assert metadata.camera.z_position_mm == 12.0
        assert metadata.plate.width_mm == spec.plate_width_mm
        assert metadata.plate.height_mm == spec.plate_height_mm
        assert metadata.plate.edge_margin_mm == spec.edge_margin_mm
        config = metadata.config
        assert config.cell_size_mm == spec.cell_size_mm
        assert config.cell_gap_mm == spec.cell_gap_mm
        assert config.purge_cell_size_mm == spec.purge_cell_size_mm
        assert config.volume_min_ul == spec.volume_min_ul
        assert config.volume_max_ul == spec.volume_max_ul
        assert config.volume_divisions == spec.volume_divisions
        assert config.samples_per_volume == spec.samples_per_volume
        assert config.blank_count == spec.blank_count
        assert config.shuffle_seed == spec.shuffle_seed
        assert config.crop_size_mm == spec.crop_size_mm
        assert config.paste_height_mm == PASTE_HEIGHT_MM
        assert config.crop_size_px == CROP_SIZE_PX
        assert config.capture_order == "phased"
        assert config.view_count == 0
        assert config.view_offset_mm == 1.0
        assert config.initial_purge_ul == 0.5
        assert metadata.plate.height_plane_z_mm == HEIGHT_PLANE_Z_MM
        assert metadata.label.kind == "rotation_allocated"
        # prime_extra_delay は dispenser 設定が 0.8 でも 0.0 固定で記録する
        assert config.prime_extra_delay_s == 0.0

    def test_record_post_reports_crop_rect_mismatch(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        cell = plan.cells[0]
        recorder.record_pre(cell, VIEW, _crop())

        error = recorder.record_post(cell, VIEW, _crop(pixel_rect=(11, 20, 19, 26)))

        assert error is not None
        assert str(cell.index) in error

    def test_record_post_without_pre_reports_missing_capture(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)

        error = recorder.record_post(plan.cells[0], VIEW, _crop())

        assert error is not None
        assert "pre" in error

    def test_mark_incomplete_keeps_session_and_metadata_stays_none(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        target = plan.targets[0]
        recorder.record_pre(target, VIEW, _crop())

        incomplete = recorder.mark_incomplete()

        assert incomplete.name.endswith(".incomplete")
        assert (incomplete / "pre" / f"{target.index:06d}.00.png").is_file()
        assert recorder.metadata is None


class TestBlankCells:
    """塗布しない blank セルは blanks[] へ真値 0 で入り、配分の分母に入らない."""

    def test_blanks_are_separated_from_the_volume_samples(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        assert [blank.index for blank in metadata.blanks] == [
            cell.index for cell in plan.blanks
        ]
        assert [sample.index for sample in metadata.samples] == [
            cell.index for cell in plan.cells
        ]

    def test_blank_measured_volume_is_exactly_zero(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        assert metadata.blanks
        assert [blank.measured_volume_ul for blank in metadata.blanks] == [0.0] * len(
            metadata.blanks
        )

    def test_blanks_are_excluded_from_the_rotation_allocation(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        total_volume = MEASURED_MASS_MG / DENSITY
        # blank を分母に入れていれば、量点 + purge の合計は総体積より小さくなる
        assert sum(
            sample.measured_volume_ul for sample in metadata.samples
        ) + metadata.purge.measured_volume_ul == pytest.approx(total_volume)

    def test_blanks_keep_their_pre_and_post_captures(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        session = recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        for blank in metadata.blanks:
            for view in blank.views:
                assert (session / view.pre).is_file()
                assert (session / view.post).is_file()


class TestDispenseOrder:
    """``samples[].order`` は塗布実行順を表し、blank は order を消費しない."""

    def test_order_matches_the_planned_dispense_order(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        assert [sample.order for sample in metadata.samples] == [
            cell.order for cell in plan.cells
        ]

    def test_order_covers_every_sample_exactly_once(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        assert sorted(sample.order for sample in metadata.samples) == list(
            range(1, len(metadata.samples) + 1)
        )

    def test_blank_cells_do_not_consume_an_order_number(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        assert plan.blanks
        recorder = _recorder(tmp_path, plan)
        _record_all(recorder, plan)

        recorder.finalize(measured_mass_mg=MEASURED_MASS_MG, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        assert max(sample.order for sample in metadata.samples) == len(metadata.samples)
        assert len(metadata.samples) < plan.spec.target_count


class TestDotCellIsTheRecordingKey:
    """Recorder は pad ではなく DotCell を受け、index で PNG を採番する."""

    def test_capture_files_are_named_by_the_cell_index(
        self, tmp_path: Path, plan: DotGridPlan
    ):
        recorder = _recorder(tmp_path, plan)
        for target in plan.targets:
            recorder.record_pre(target, VIEW, _crop())

        session = recorder.mark_incomplete()

        assert {path.name for path in (session / "pre").iterdir()} == {
            f"{target.index:06d}.00.png" for target in plan.targets
        }
