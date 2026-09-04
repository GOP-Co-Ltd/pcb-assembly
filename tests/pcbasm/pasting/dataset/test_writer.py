"""Dataset session writer（採番・lossless PNG・atomic 確定）の公開契約."""

import json
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest
from shapely import box

from pcbasm.pasting.applicator import DispenseSummary
from pcbasm.pasting.dataset.metadata import (
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
)
from pcbasm.pasting.dataset.writer import PasteDatasetWriter
from pcbasm.pasting.fill_path import AppliedDispenseMode
from pcbasm.pasting.params import PasteParams
from pcbasm.vision.crop import PolygonCrop

STARTED_AT = datetime(2026, 8, 28, 14, 30, 52, 123456, tzinfo=UTC)


def _source_image(width: int = 8, height: int = 6) -> np.ndarray:
    yy, xx = np.indices((height, width), dtype=np.uint8)
    return np.dstack((xx, yy, xx ^ yy))


def _summary(applied_mode: AppliedDispenseMode, rotations: float) -> DispenseSummary:
    return DispenseSummary(
        applied_mode=applied_mode,
        path_length_mm=4.0,
        commanded_volume_ul=0.15,
        prime_extra_volume_ul=0.0,
        effective_rate_ul_s=1.0,
        rotations=rotations,
    )


def _metadata(view: DatasetCapturedView) -> PasteDatasetMetadata:
    """Purge を sample から除外し、通常 pad だけを含む最小 schema v1."""
    return PasteDatasetMetadata(
        kind="pcbasm-paste-volume-dataset",
        schema_version=1,
        created_at="2026-08-28T14:30:52+00:00",
        machine=PasteDatasetMachine(machine_id="machine-1", name="Machine 1"),
        board=PasteDatasetBoard(
            filename="arbitrary.kicad_pcb",
            source_pcb="/boards/arbitrary.kicad_pcb",
            signature="board-signature",
        ),
        paste=PasteDatasetPaste(
            paste_id="paste-1", lot="lot-1", density_mg_per_ul=3.78
        ),
        camera=PasteDatasetCamera(
            pixel_per_mm=120.5,
            resolution=(120, 100),
            calibrated_at="2026-08-20T12:00:00+00:00",
            z_position_mm=12.0,
        ),
        nozzle=PasteDatasetNozzle(diameter_mm=0.34),
        config=PasteDatasetConfig(
            rotations_per_ul=20.0,
            max_fill_speed_mm_s=2.0,
            max_dispense_rate_ul_s=5.0,
            dispense_accel_ul_s2=10.0,
            retract_amount_ul=10.0,
            retract_rate_ul_s=10.0,
            initial_purge_ul=0.1,
            crop_margin_mm=1.0,
            mask_margin_mm=0.1,
        ),
        total=PasteDatasetTotal(
            measured_mass_mg=0.945,
            measured_volume_ul=0.25,
            rotations=5.0,
        ),
        purge=PasteDatasetPurge(
            pad_id="PURGE",
            source_pad_id="PURGE.1",
            execution=_summary("dot", 2.0),
            measured_volume_ul=0.1,
        ),
        pads=(
            PasteDatasetPad(
                index=1,
                pad_id="U1.1",
                source_pad_id="U1.1",
                polygon=DatasetPolygon.from_polygon(box(0.0, 0.0, 2.0, 1.0)),
                resolved=PasteParams(
                    dispense_mode="area",
                    line_direction="unconstrained",
                    ul_per_mm2=0.05,
                    paste_height=0.05,
                    prime_extra_delay=0.0,
                    bead_width_factor=1.0,
                    overlap=0.0,
                    boundary_margin=0.0,
                ),
                execution=_summary("area", 3.0),
                measured_volume_ul=0.15,
                views=(view,),
            ),
        ),
    )


@pytest.fixture
def crop() -> PolygonCrop:
    return PolygonCrop(
        image=_source_image(),
        mask=np.array(
            [
                [0, 0, 0, 0, 0, 0, 0, 0],
                [0, 255, 255, 255, 255, 255, 255, 0],
                [0, 255, 255, 0, 0, 255, 255, 0],
                [0, 255, 255, 0, 0, 255, 255, 0],
                [0, 255, 255, 255, 255, 255, 255, 0],
                [0, 0, 0, 0, 0, 0, 0, 0],
            ],
            dtype=np.uint8,
        ),
        pixel_rect=(10, 20, 18, 26),
    )


class TestPasteDatasetWriterOpen:
    """Open() が root / 一時 session directory を作り、同名衝突を採番する."""

    def test_creates_root_and_phase_directories(self, tmp_path: Path):
        root = tmp_path / "datasets"

        writer = PasteDatasetWriter.open(
            root, board_name="arbitrary", started_at=STARTED_AT
        )

        assert writer.root == root
        assert writer.working_path == root / ".arbitrary-20260828T143052.123+0000.tmp"
        assert {p.name for p in writer.working_path.iterdir()} == {
            "pre",
            "post",
            "mask",
        }

    def test_same_board_and_millisecond_gets_numeric_suffix(self, tmp_path: Path):
        first = PasteDatasetWriter.open(
            tmp_path, board_name="arbitrary", started_at=STARTED_AT
        )
        second = PasteDatasetWriter.open(
            tmp_path, board_name="arbitrary", started_at=STARTED_AT
        )

        assert first.working_path.name == ".arbitrary-20260828T143052.123+0000.tmp"
        assert second.working_path.name == ".arbitrary-20260828T143052.123+0000-1.tmp"

    def test_rejects_naive_started_at_and_empty_board_name(self, tmp_path: Path):
        with pytest.raises(ValueError):
            PasteDatasetWriter.open(
                tmp_path, board_name="arbitrary", started_at=datetime(2026, 8, 28)
            )
        with pytest.raises(ValueError):
            PasteDatasetWriter.open(tmp_path, board_name="", started_at=STARTED_AT)


class TestPasteDatasetWriterCaptures:
    """Lossless PNG の命名と中断 session 保持."""

    def test_mark_incomplete_keeps_captured_files(
        self, tmp_path: Path, crop: PolygonCrop
    ):
        writer = PasteDatasetWriter.open(
            tmp_path,
            board_name="arbitrary",
            started_at=datetime(2026, 8, 28, 14, 30, 52, tzinfo=UTC),
        )
        view = DatasetView(number=0)
        writer.write_capture(1, view, "pre", crop)
        writer.write_capture(1, view, "post", crop)

        session = writer.mark_incomplete()

        assert session.parent == tmp_path
        assert session.name == "arbitrary-20260828T143052.000+0000.incomplete"
        expected = Path("000001.00.png")
        pre = cv2.imread(str(session / "pre" / expected), cv2.IMREAD_UNCHANGED)
        post = cv2.imread(str(session / "post" / expected), cv2.IMREAD_UNCHANGED)
        mask = cv2.imread(str(session / "mask" / expected), cv2.IMREAD_UNCHANGED)
        assert pre is not None
        assert post is not None
        assert mask is not None
        assert np.array_equal(pre, crop.image)
        assert np.array_equal(post, crop.image)
        assert np.array_equal(mask, crop.mask)

    def test_rejects_duplicate_capture(self, tmp_path: Path, crop: PolygonCrop):
        writer = PasteDatasetWriter.open(tmp_path, board_name="arbitrary")
        view = DatasetView(number=0)
        writer.write_capture(1, view, "pre", crop)

        with pytest.raises(ValueError):
            writer.write_capture(1, view, "pre", crop)

    def test_finalize_writes_schema_and_atomically_publishes_session(
        self, tmp_path: Path, crop: PolygonCrop
    ):
        writer = PasteDatasetWriter.open(
            tmp_path, board_name="arbitrary", started_at=STARTED_AT
        )
        view = DatasetView(number=0)
        captured = writer.write_capture(1, view, "pre", crop)
        writer.write_capture(1, view, "post", crop)

        session = writer.finalize(_metadata(captured))

        assert session.name == "arbitrary-20260828T143052.123+0000"
        assert {path.name for path in tmp_path.iterdir()} == {session.name}
        payload = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
        assert payload["kind"] == "pcbasm-paste-volume-dataset"
        assert payload["schema_version"] == 1
        assert payload["purge"]["pad_id"] == "PURGE"
        assert payload["purge"]["source_pad_id"] == "PURGE.1"
        assert [pad["pad_id"] for pad in payload["pads"]] == ["U1.1"]
        assert payload["pads"][0]["views"] == [
            {
                "number": 0,
                "offset_x_mm": 0.0,
                "offset_y_mm": 0.0,
                "pixel_rect": [10, 20, 18, 26],
                "pre": "pre/000001.00.png",
                "post": "post/000001.00.png",
                "mask": "mask/000001.00.png",
            }
        ]

    def test_finalize_rejects_missing_post_capture(
        self, tmp_path: Path, crop: PolygonCrop
    ):
        writer = PasteDatasetWriter.open(tmp_path, board_name="arbitrary")
        captured = writer.write_capture(1, DatasetView(number=0), "pre", crop)

        with pytest.raises(ValueError):
            writer.finalize(_metadata(captured))


class TestPasteDatasetWriterContextManager:
    """Finalize せずに抜けた session は incomplete として保持される."""

    def test_unfinalized_session_is_marked_incomplete_on_exit(
        self, tmp_path: Path, crop: PolygonCrop
    ):
        with PasteDatasetWriter.open(
            tmp_path, board_name="arbitrary", started_at=STARTED_AT
        ) as writer:
            writer.write_capture(1, DatasetView(number=0), "pre", crop)

        assert {path.name for path in tmp_path.iterdir()} == {
            "arbitrary-20260828T143052.123+0000.incomplete"
        }

    def test_exception_inside_block_keeps_incomplete_session(
        self, tmp_path: Path, crop: PolygonCrop
    ):
        with pytest.raises(RuntimeError):
            with PasteDatasetWriter.open(
                tmp_path, board_name="arbitrary", started_at=STARTED_AT
            ) as writer:
                writer.write_capture(1, DatasetView(number=0), "pre", crop)
                raise RuntimeError("abort")

        incomplete = tmp_path / "arbitrary-20260828T143052.123+0000.incomplete"
        assert (incomplete / "pre" / "000001.00.png").is_file()

    def test_finalized_session_is_not_marked_incomplete(
        self, tmp_path: Path, crop: PolygonCrop
    ):
        with PasteDatasetWriter.open(
            tmp_path, board_name="arbitrary", started_at=STARTED_AT
        ) as writer:
            view = DatasetView(number=0)
            captured = writer.write_capture(1, view, "pre", crop)
            writer.write_capture(1, view, "post", crop)
            session = writer.finalize(_metadata(captured))

        assert {path.name for path in tmp_path.iterdir()} == {session.name}
