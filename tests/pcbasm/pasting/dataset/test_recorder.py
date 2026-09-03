"""Dataset recorder（撮影・塗布実績の蓄積 → metadata.json 組立）の公開契約.

実 PCB（led_blinker）から ``plan_dataset_targets`` で対象を解決し、record_* → finalize が
現行 metadata.json と同じキー集合（``data/testing/schemas/paste_dataset_metadata_v1.json``）
を書くことを検証する。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from pcbasm.config import PasteDispenser as PasteDispenserConfig, Toolhead
from pcbasm.pasting.applicator import DispenseExecution, PasteApplicationResult
from pcbasm.pasting.dataset.metadata import DatasetView, parse_metadata
from pcbasm.pasting.dataset.recorder import (
    DatasetRunInfo,
    PasteDatasetRecorder,
    validate_dataset_run,
)
from pcbasm.pasting.dataset.writer import PasteDatasetWriter
from pcbasm.pasting.params import PasteParams
from pcbasm.pasting.settings import PasteSettingsModel
from pcbasm.pasting.workflow import DatasetTargets, plan_dataset_targets
from pcbasm.pcb import PcbFile, build_pad_hierarchy
from pcbasm.vision.calibration import CalibrationResult
from pcbasm.vision.crop import PolygonCrop
from tests.helpers import TESTING_DATA_DIR

LED_BLINKER = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
METADATA_V1 = TESTING_DATA_DIR / "schemas" / "paste_dataset_metadata_v1.json"
STARTED_AT = datetime(2026, 8, 28, 14, 30, 52, 123456, tzinfo=UTC)
DENSITY = 3.78


def _crop(pixel_rect: tuple[int, int, int, int] = (10, 20, 18, 26)) -> PolygonCrop:
    image = np.full((6, 8, 3), 127, dtype=np.uint8)
    mask = np.zeros((6, 8), dtype=np.uint8)
    mask[1:5, 1:7] = 255
    return PolygonCrop(image=image, mask=mask, pixel_rect=pixel_rect)


def _execution(applied_mode: str, rotations: float) -> PasteApplicationResult:
    return PasteApplicationResult(
        (
            DispenseExecution(
                applied_mode=applied_mode,  # type: ignore[arg-type]
                path_length_mm=1.0,
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
        ul_per_mm2=0.05,
        solder_paste_density=DENSITY,
        initial_purge_ul=0.5,
    )


def _run_info() -> DatasetRunInfo:
    return DatasetRunInfo(
        machine_id="machine-1",
        machine_name="Machine 1",
        pcb_filename=LED_BLINKER.name,
        source_pcb="real/led_blinker.kicad_pcb",
        paste_id="paste-1",
        paste_lot=None,
        crop_margin_mm=1.0,
        mask_margin_mm=0.1,
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
def targets() -> DatasetTargets:
    pcb = PcbFile(LED_BLINKER)
    hierarchy = build_pad_hierarchy(pcb.components, pcb.pads)
    base = PasteParams(
        dispense_mode="auto",
        line_direction="unconstrained",
        ul_per_mm2=0.1,
        paste_height="auto",
        prime_extra_delay=0.8,
        bead_width_factor=1.0,
        overlap=0.0,
        boundary_margin=0.0,
    )
    model = (
        PasteSettingsModel(base=base)
        .with_initial_purge_pad_id("U1.1")
        .with_level_patch(
            hierarchy.l4_key_for_pad_id("D1.1"), values={"ul_per_mm2": 0.25}
        )
    )
    planned, error = plan_dataset_targets(pcb, hierarchy, model, initial_purge_ul=0.5)
    assert error is None
    assert planned is not None
    return planned


def _record_all(
    recorder: PasteDatasetRecorder, targets: DatasetTargets, view: DatasetView
) -> None:
    for index, pad in enumerate(targets.sample_pads, start=1):
        recorder.record_pre(index, pad, view, _crop())
    recorder.record_execution(targets.purge_pad_id, _execution("dot", 2.0))
    for pad in targets.sample_pads:
        recorder.record_execution(
            targets.hierarchy.pad_id_for_pad(pad), _execution("area", 3.0)
        )
    for index, pad in enumerate(targets.sample_pads, start=1):
        assert recorder.record_post(index, pad, view, _crop()) is None


class TestValidateDatasetRun:
    def test_accepts_positive_purge_paste_id_and_ordered_margins(self):
        assert (
            validate_dataset_run(
                initial_purge_ul=0.5,
                paste_id="paste-1",
                crop_margin_mm=1.0,
                mask_margin_mm=0.1,
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
            ({"crop_margin_mm": -1.0}, "crop_margin_mm"),
            ({"crop_margin_mm": "1.0"}, "crop_margin_mm"),
            ({"mask_margin_mm": 2.0}, "mask_margin_mm"),
        ],
    )
    def test_reports_first_invalid_condition(
        self, overrides: dict[str, object], expected: str
    ):
        params: dict[str, object] = {
            "initial_purge_ul": 0.5,
            "paste_id": "paste-1",
            "crop_margin_mm": 1.0,
            "mask_margin_mm": 0.1,
        }
        params.update(overrides)

        error = validate_dataset_run(**params)  # type: ignore[arg-type]

        assert error is not None
        assert expected in error


class TestPasteDatasetRecorder:
    """record_pre / record_execution / record_post → finalize の metadata 組立."""

    def test_finalize_writes_metadata_with_current_key_set(
        self, tmp_path: Path, targets: DatasetTargets
    ):
        view = DatasetView(number=0)
        recorder = PasteDatasetRecorder(
            PasteDatasetWriter.open(
                tmp_path, board_name="led_blinker", started_at=STARTED_AT
            ),
            targets,
            (view,),
        )
        _record_all(recorder, targets, view)

        session = recorder.finalize(measured_mass_mg=1.89, run=_run_info())

        payload = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
        expected = json.loads(METADATA_V1.read_text(encoding="utf-8"))
        assert _key_paths(payload) == _key_paths(expected)
        parsed, error = parse_metadata(payload)
        assert error is None
        assert parsed is not None
        assert recorder.metadata == parsed

    def test_finalize_allocates_measured_volume_by_rotations(
        self, tmp_path: Path, targets: DatasetTargets
    ):
        view = DatasetView(number=0)
        recorder = PasteDatasetRecorder(
            PasteDatasetWriter.open(
                tmp_path, board_name="led_blinker", started_at=STARTED_AT
            ),
            targets,
            (view,),
        )
        _record_all(recorder, targets, view)

        recorder.finalize(measured_mass_mg=1.89, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        total_volume = 1.89 / DENSITY
        pad_count = len(targets.sample_pads)
        total_rotations = 2.0 + 3.0 * pad_count
        assert metadata.total.measured_mass_mg == 1.89
        assert metadata.total.measured_volume_ul == pytest.approx(total_volume)
        assert metadata.total.rotations == pytest.approx(total_rotations)
        assert metadata.purge.pad_id == "U1.1"
        assert metadata.purge.measured_volume_ul == pytest.approx(
            total_volume * 2.0 / total_rotations
        )
        assert len(metadata.pads) == pad_count
        assert sum(pad.measured_volume_ul for pad in metadata.pads) + (
            metadata.purge.measured_volume_ul
        ) == pytest.approx(total_volume)
        assert [pad.index for pad in metadata.pads] == list(range(1, pad_count + 1))

    def test_finalize_records_resolved_params_execution_and_run_info(
        self, tmp_path: Path, targets: DatasetTargets
    ):
        view = DatasetView(number=0)
        recorder = PasteDatasetRecorder(
            PasteDatasetWriter.open(
                tmp_path, board_name="led_blinker", started_at=STARTED_AT
            ),
            targets,
            (view,),
        )
        _record_all(recorder, targets, view)

        recorder.finalize(measured_mass_mg=1.89, run=_run_info())

        metadata = recorder.metadata
        assert metadata is not None
        by_id = {pad.pad_id: pad for pad in metadata.pads}
        assert by_id["D1.1"].resolved.ul_per_mm2 == 0.25
        assert by_id["D1.2"].resolved.ul_per_mm2 == 0.1
        assert by_id["D1.1"].resolved.paste_height == "auto"
        assert by_id["D1.1"].execution.applied_mode == "area"
        assert by_id["D1.1"].execution.rotations == 3.0
        assert by_id["D1.1"].views[0].pixel_rect == (10, 20, 18, 26)
        assert metadata.created_at == STARTED_AT.isoformat()
        assert metadata.machine.machine_id == "machine-1"
        assert metadata.board.filename == "led_blinker.kicad_pcb"
        assert metadata.board.signature == targets.hierarchy.signature()
        assert metadata.paste.density_mg_per_ul == DENSITY
        assert metadata.camera.z_position_mm == 12.0
        assert metadata.config.initial_purge_ul == 0.5
        assert metadata.config.crop_margin_mm == 1.0

    def test_record_post_reports_crop_rect_mismatch(
        self, tmp_path: Path, targets: DatasetTargets
    ):
        view = DatasetView(number=0)
        recorder = PasteDatasetRecorder(
            PasteDatasetWriter.open(tmp_path, board_name="led_blinker"),
            targets,
            (view,),
        )
        pad = targets.sample_pads[0]
        recorder.record_pre(1, pad, view, _crop())

        error = recorder.record_post(1, pad, view, _crop(pixel_rect=(11, 20, 19, 26)))

        assert error is not None
        assert targets.hierarchy.pad_id_for_pad(pad) in error

    def test_record_post_without_pre_reports_missing_capture(
        self, tmp_path: Path, targets: DatasetTargets
    ):
        view = DatasetView(number=0)
        recorder = PasteDatasetRecorder(
            PasteDatasetWriter.open(tmp_path, board_name="led_blinker"),
            targets,
            (view,),
        )

        error = recorder.record_post(1, targets.sample_pads[0], view, _crop())

        assert error is not None
        assert "pre" in error

    def test_mark_incomplete_keeps_session_and_metadata_stays_none(
        self, tmp_path: Path, targets: DatasetTargets
    ):
        view = DatasetView(number=0)
        recorder = PasteDatasetRecorder(
            PasteDatasetWriter.open(
                tmp_path, board_name="led_blinker", started_at=STARTED_AT
            ),
            targets,
            (view,),
        )
        recorder.record_pre(1, targets.sample_pads[0], view, _crop())

        incomplete = recorder.mark_incomplete()

        assert incomplete.name.endswith(".incomplete")
        assert (incomplete / "pre" / "000001.00.png").is_file()
        assert recorder.metadata is None
