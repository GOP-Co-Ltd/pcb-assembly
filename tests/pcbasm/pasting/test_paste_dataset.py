"""ペースト塗布画像datasetの公開API仕様テスト."""

import json
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest
from shapely import Polygon, box

from pcbasm.pasting.paste_dataset import (
    DatasetCapturedView,
    DatasetExecution,
    DatasetPolygon,
    DatasetResolvedPaste,
    DatasetView,
    PadImageCrop,
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
    PasteDatasetWriter,
    allocate_volume_by_rotations,
    crop_pad_image,
)
from pcbasm.vision import Image


def _source_image(width: int = 120, height: int = 100) -> np.ndarray:
    """切り抜き位置とRGB保持を同時に確認できる合成画像."""
    yy, xx = np.indices((height, width), dtype=np.uint8)
    return np.dstack((xx, yy, xx ^ yy))


def _metadata(view: DatasetCapturedView) -> PasteDatasetMetadata:
    """Purgeをsampleから除外し、通常padだけを含む最小schema v1."""
    purge_execution = DatasetExecution(
        applied_mode="dot",
        path_length_mm=0.0,
        commanded_volume_ul=0.1,
        prime_extra_volume_ul=0.0,
        effective_rate_ul_s=1.0,
        rotations=2.0,
    )
    pad_execution = DatasetExecution(
        applied_mode="area",
        path_length_mm=4.0,
        commanded_volume_ul=0.15,
        prime_extra_volume_ul=0.0,
        effective_rate_ul_s=1.0,
        rotations=3.0,
    )
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
        ),
        total=PasteDatasetTotal(
            measured_mass_mg=0.945,
            measured_volume_ul=0.25,
            rotations=5.0,
        ),
        purge=PasteDatasetPurge(
            pad_id="PURGE",
            source_pad_id="PURGE.1",
            execution=purge_execution,
            measured_volume_ul=0.1,
        ),
        pads=(
            PasteDatasetPad(
                index=1,
                pad_id="U1.1",
                source_pad_id="U1.1",
                polygon=DatasetPolygon.from_polygon(box(0.0, 0.0, 2.0, 1.0)),
                resolved=DatasetResolvedPaste(
                    dispense_mode="area",
                    line_direction="unconstrained",
                    paste_height=0.05,
                    ul_per_mm2=0.05,
                    prime_extra_delay=0.0,
                    bead_width_factor=1.0,
                    overlap=0.0,
                    boundary_margin=0.0,
                ),
                execution=pad_execution,
                measured_volume_ul=0.15,
                views=(view,),
            ),
        ),
    )


class TestDatasetView:
    """1つのpadに対する撮影位置の公開表現."""

    def test_central_view_defaults_to_zero_offsets(self):
        view = DatasetView(number=0)

        assert view.number == 0
        assert view.offset_x_mm == 0.0
        assert view.offset_y_mm == 0.0

    def test_additional_view_keeps_number_and_offsets(self):
        view = DatasetView(number=1, offset_x_mm=2.0, offset_y_mm=-1.5)

        assert view.number == 1
        assert view.offset_x_mm == 2.0
        assert view.offset_y_mm == -1.5


class TestCropPadImage:
    """F.Paste polygonのAABB cropと専用mask生成."""

    def test_crops_rgb_by_pad_bounds_plus_margin(self):
        source = _source_image()
        polygon = box(-1.0, -2.0, 1.0, 2.0)
        matrix = np.array([[10.0, 0.0], [0.0, 10.0]])
        shift = np.array([50.0, 40.0])

        crop = crop_pad_image(Image(source), polygon, matrix, shift, margin_mm=1.0)

        assert crop.pixel_rect == (30, 10, 70, 70)
        assert crop.image.shape == (60, 40, 3)
        assert crop.image.dtype == np.uint8
        assert np.array_equal(crop.image, source[10:70, 30:70])
        assert crop.mask.shape == crop.image.shape[:2]
        assert crop.mask.dtype == np.uint8
        assert set(np.unique(crop.mask)) <= {0, 255}

    def test_mask_preserves_polygon_holes(self):
        source = _source_image()
        polygon = Polygon(
            [(-2.0, -2.0), (2.0, -2.0), (2.0, 2.0), (-2.0, 2.0)],
            holes=[[(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]],
        )

        crop = crop_pad_image(
            source,
            polygon,
            np.array([[10.0, 0.0], [0.0, 10.0]]),
            np.array([50.0, 40.0]),
            margin_mm=0.0,
        )

        assert crop.mask[20, 20] == 0
        assert crop.mask[5, 5] == 255

    @pytest.mark.parametrize("margin_mm", [-0.01, -1.0])
    def test_rejects_negative_margin(self, margin_mm: float):
        with pytest.raises(ValueError):
            crop_pad_image(
                _source_image(),
                box(-1.0, -1.0, 1.0, 1.0),
                np.eye(2),
                np.array([50.0, 40.0]),
                margin_mm=margin_mm,
            )

    def test_rejects_crop_outside_camera_frame(self):
        with pytest.raises(ValueError):
            crop_pad_image(
                _source_image(),
                box(-2.0, -2.0, 2.0, 2.0),
                np.array([[10.0, 0.0], [0.0, 10.0]]),
                np.array([5.0, 5.0]),
                margin_mm=0.0,
            )


class TestAllocateVolumeByRotations:
    """purgeを分母に含めた教師体積の比例配分."""

    def test_allocates_total_volume_including_purge_share(self):
        allocated = allocate_volume_by_rotations(
            2.0,
            {"PURGE": 2.0, "U1.1": 3.0, "U2.1": 5.0},
        )

        assert allocated == pytest.approx({"PURGE": 0.4, "U1.1": 0.6, "U2.1": 1.0})

    @pytest.mark.parametrize(
        ("total_volume_ul", "rotations"),
        [
            (0.0, {"PURGE": 1.0}),
            (-0.1, {"PURGE": 1.0}),
            (1.0, {}),
            (1.0, {"PURGE": 0.0}),
            (1.0, {"PURGE": -1.0, "U1.1": 2.0}),
        ],
    )
    def test_rejects_non_positive_measurements(
        self, total_volume_ul: float, rotations: dict[str, float]
    ):
        with pytest.raises(ValueError):
            allocate_volume_by_rotations(total_volume_ul, rotations)


class TestPasteDatasetMetadata:
    """Schema v1は未知keyや暗黙の型変換を受理しない."""

    @pytest.fixture
    def payload(self) -> dict[str, object]:
        view = DatasetCapturedView(
            number=0,
            offset_x_mm=0.0,
            offset_y_mm=0.0,
            pixel_rect=(10, 20, 18, 26),
            pre="pre/000001.00.png",
            post="post/000001.00.png",
            mask="mask/000001.00.png",
        )
        return _metadata(view).to_dict()

    def test_accepts_exact_schema(self, payload: dict[str, object]):
        metadata = PasteDatasetMetadata.from_dict(payload)

        assert metadata.to_dict() == payload

    def test_accepts_missing_manufacturing_lot_as_null(
        self, payload: dict[str, object]
    ):
        paste = payload["paste"]
        assert isinstance(paste, dict)
        paste["lot"] = None

        metadata = PasteDatasetMetadata.from_dict(payload)

        assert metadata.paste.lot is None
        assert metadata.to_dict()["paste"]["lot"] is None

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("schema_version",), "1"),
            (("paste", "lot"), 123),
            (("paste", "density_mg_per_ul"), 3),
            (("camera", "pixel_per_mm"), True),
        ],
    )
    def test_rejects_implicit_types(
        self, payload: dict[str, object], path: tuple[str, ...], value: object
    ):
        target = payload
        for key in path[:-1]:
            nested = target[key]
            assert isinstance(nested, dict)
            target = nested
        target[path[-1]] = value

        with pytest.raises(ValueError):
            PasteDatasetMetadata.from_dict(payload)

    @pytest.mark.parametrize("nested", [False, True])
    def test_rejects_unknown_keys(self, payload: dict[str, object], nested: bool):
        target = payload
        if nested:
            paste = target["paste"]
            assert isinstance(paste, dict)
            target = paste
        target["unexpected"] = "value"

        with pytest.raises(ValueError):
            PasteDatasetMetadata.from_dict(payload)


class TestPasteDatasetWriter:
    """Lossless PNGの命名と中断session保持."""

    @pytest.fixture
    def crop(self) -> PadImageCrop:
        return PadImageCrop(
            image=_source_image(width=8, height=6),
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

    def test_mark_incomplete_keeps_captured_files(
        self, tmp_path: Path, crop: PadImageCrop
    ):
        writer = PasteDatasetWriter(
            tmp_path, started_at=datetime(2026, 8, 28, 14, 30, 52, tzinfo=UTC)
        )
        view = DatasetView(number=0)
        writer.write_capture(1, view, "pre", crop)
        writer.write_capture(1, view, "post", crop)

        session = writer.mark_incomplete()

        assert session.parent == tmp_path
        assert session.name.endswith(".incomplete")
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

    def test_rejects_duplicate_capture(self, tmp_path: Path, crop: PadImageCrop):
        writer = PasteDatasetWriter(tmp_path)
        view = DatasetView(number=0)
        writer.write_capture(1, view, "pre", crop)

        with pytest.raises(ValueError):
            writer.write_capture(1, view, "pre", crop)

    def test_finalize_writes_schema_and_atomically_publishes_session(
        self, tmp_path: Path, crop: PadImageCrop
    ):
        writer = PasteDatasetWriter(
            tmp_path,
            started_at=datetime(2026, 8, 28, 14, 30, 52, 123456, tzinfo=UTC),
        )
        view = DatasetView(number=0)
        captured = writer.write_capture(1, view, "pre", crop)
        writer.write_capture(1, view, "post", crop)

        session = writer.finalize(_metadata(captured))

        assert session.name == "20260828T143052.123456+0000"
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
        self, tmp_path: Path, crop: PadImageCrop
    ):
        writer = PasteDatasetWriter(tmp_path)
        captured = writer.write_capture(1, DatasetView(number=0), "pre", crop)

        with pytest.raises(ValueError):
            writer.finalize(_metadata(captured))
