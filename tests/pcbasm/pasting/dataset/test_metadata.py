"""Dataset metadata schema v2（DTO・strict parse・体積配分・view 検証）の公開契約.

``data/testing/schemas/paste_dataset_metadata_v2.json`` が on-disk 形状のピン。

v1 からの破壊的変更（``board`` → ``plate``、``pads`` → ``samples``、mask 廃止、
点塗布固有 config）を固定し、``schema_version: 1`` の doc は移行せず拒否する。
"""

import json
from pathlib import Path

import pytest

from pcbasm.geometry import Point2d
from pcbasm.geometry.packing import Rect
from pcbasm.pasting.dataset.metadata import (
    METADATA_SCHEMA_VERSION,
    DatasetView,
    PasteDatasetMetadata,
    allocate_volume_by_rotations,
    parse_metadata,
)
from pcbasm.pasting.dispense import DispenseSummary
from tests.helpers import TESTING_DATA_DIR

METADATA_V1 = TESTING_DATA_DIR / "schemas" / "paste_dataset_metadata_v1.json"
METADATA_V2 = TESTING_DATA_DIR / "schemas" / "paste_dataset_metadata_v2.json"

CONFIG_KEYS = {
    "rotations_per_ul",
    "max_dispense_rate_ul_s",
    "dispense_accel_ul_s2",
    "retract_amount_ul",
    "retract_rate_ul_s",
    "initial_purge_ul",
    "paste_height_mm",
    "prime_extra_delay_s",
    "capture_order",
    "cell_size_mm",
    "cell_gap_mm",
    "purge_cell_size_mm",
    "crop_size_mm",
    "crop_size_px",
    "volume_min_ul",
    "volume_max_ul",
    "volume_divisions",
    "samples_per_volume",
    "blank_count",
    "shuffle_seed",
    "view_count",
    "view_offset_mm",
}
SAMPLE_KEYS = {
    "index",
    "order",
    "cell",
    "center",
    "commanded_volume_ul",
    "volume_index",
    "execution",
    "measured_volume_ul",
    "views",
}
BLANK_KEYS = {
    "index",
    "cell",
    "center",
    "measured_volume_ul",
    "views",
}
VIEW_KEYS = {
    "number",
    "offset_x_mm",
    "offset_y_mm",
    "pixel_rect",
    "pre",
    "post",
}


def _load_v2() -> dict[str, object]:
    return json.loads(METADATA_V2.read_text(encoding="utf-8"))


def _json_roundtrip(metadata: PasteDatasetMetadata) -> dict[str, object]:
    return json.loads(json.dumps(metadata.to_dict()))


def _first_blank(payload: dict[str, object]) -> dict[str, object]:
    blanks = payload["blanks"]
    assert isinstance(blanks, list)
    blank = blanks[0]
    assert isinstance(blank, dict)
    return blank


def _first_sample(payload: dict[str, object]) -> dict[str, object]:
    samples = payload["samples"]
    assert isinstance(samples, list)
    sample = samples[0]
    assert isinstance(sample, dict)
    return sample


class TestDatasetView:
    """1 セルに対する撮影位置の公開表現と検証."""

    def test_central_view_defaults_to_zero_offsets(self):
        view = DatasetView(number=0)

        assert view.offset_x_mm == 0.0
        assert view.offset_y_mm == 0.0
        assert view.validate() is None

    def test_peripheral_view_keeps_number_and_offsets(self):
        view = DatasetView(number=1, offset_x_mm=2.0, offset_y_mm=-1.5)

        assert view.validate() is None
        assert view.number == 1
        assert view.offset_x_mm == 2.0
        assert view.offset_y_mm == -1.5

    @pytest.mark.parametrize(
        ("view", "expected"),
        [
            (DatasetView(number=-1), "view number"),
            (DatasetView(number=0, offset_x_mm=float("nan")), "offset_x_mm"),
            (DatasetView(number=0, offset_y_mm=float("inf")), "offset_y_mm"),
        ],
    )
    def test_rejects_negative_number_and_non_finite_offsets(
        self, view: DatasetView, expected: str
    ):
        error = view.validate()

        assert error is not None
        assert expected in error


class TestAllocateVolumeByRotations:
    """Purge を分母に含めた教師体積の比例配分（量が異なるサンプルでも同じ規則）."""

    def test_allocates_total_volume_including_purge_share(self):
        allocated = allocate_volume_by_rotations(
            2.0,
            {"purge": 2.0, "000001": 3.0, "000002": 5.0},
        )

        assert allocated == pytest.approx({"purge": 0.4, "000001": 0.6, "000002": 1.0})

    @pytest.mark.parametrize(
        ("total_volume_ul", "rotations"),
        [
            (0.0, {"purge": 1.0}),
            (-0.1, {"purge": 1.0}),
            (1.0, {}),
            (1.0, {"purge": 0.0}),
            (1.0, {"purge": -1.0, "000001": 2.0}),
        ],
    )
    def test_rejects_non_positive_measurements(
        self, total_volume_ul: float, rotations: dict[str, float]
    ):
        with pytest.raises(ValueError):
            allocate_volume_by_rotations(total_volume_ul, rotations)


class TestMetadataOnDiskShape:
    """On-disk の v2 形状（キー集合・セル矩形の object 形式）をピンする."""

    @pytest.fixture
    def payload(self) -> dict[str, object]:
        return _load_v2()

    def test_fixture_file_declares_kind_and_current_schema_version(self):
        assert Path(METADATA_V2).is_file()
        payload = _load_v2()

        assert payload["kind"] == "pcbasm-paste-volume-dataset"
        assert payload["schema_version"] == METADATA_SCHEMA_VERSION
        assert METADATA_SCHEMA_VERSION == 2

    def test_top_level_sections_replace_board_with_plate_and_pads_with_samples(
        self, payload: dict[str, object]
    ):
        assert set(payload) == {
            "kind",
            "schema_version",
            "created_at",
            "machine",
            "plate",
            "paste",
            "camera",
            "nozzle",
            "label",
            "config",
            "total",
            "purge",
            "samples",
            "blanks",
        }

    def test_plate_section_carries_dimensions_and_edge_margin(
        self, payload: dict[str, object]
    ):
        assert payload["plate"] == {
            "width_mm": 40.0,
            "height_mm": 40.0,
            "edge_margin_mm": 2.0,
            "height_plane_z_mm": 1.62,
        }

    def test_config_holds_dot_grid_sweep_and_crop_settings(
        self, payload: dict[str, object]
    ):
        config = payload["config"]
        assert isinstance(config, dict)

        assert set(config) == CONFIG_KEYS

    def test_sample_replaces_pad_identity_with_cell_geometry_and_volume(
        self, payload: dict[str, object]
    ):
        sample = _first_sample(payload)

        assert set(sample) == SAMPLE_KEYS
        assert set(sample["cell"]) == {"x", "y", "width", "height"}  # type: ignore[arg-type]
        assert set(sample["center"]) == {"x", "y"}  # type: ignore[arg-type]

    def test_view_no_longer_carries_a_mask_path(self, payload: dict[str, object]):
        views = _first_sample(payload)["views"]
        assert isinstance(views, list)

        for view in views:
            assert isinstance(view, dict)
            assert set(view) == VIEW_KEYS
            assert "mask" not in view

    def test_purge_is_identified_by_cell_rect_and_center(
        self, payload: dict[str, object]
    ):
        purge = payload["purge"]
        assert isinstance(purge, dict)

        assert set(purge) == {"cell", "center", "execution", "measured_volume_ul"}

    def test_blank_entry_has_no_execution_and_zero_measured_volume(
        self, payload: dict[str, object]
    ):
        blank = _first_blank(payload)

        assert set(blank) == BLANK_KEYS
        assert "execution" not in blank
        assert "commanded_volume_ul" not in blank
        assert blank["measured_volume_ul"] == 0.0

    def test_label_section_records_how_the_truth_was_derived(
        self, payload: dict[str, object]
    ):
        assert payload["label"] == {"kind": "rotation_allocated"}

    def test_config_records_the_phased_capture_order(self, payload: dict[str, object]):
        config = payload["config"]
        assert isinstance(config, dict)

        assert config["capture_order"] == "phased"

    def test_execution_keys_match_dispense_summary(self, payload: dict[str, object]):
        assert set(_first_sample(payload)["execution"]) == {  # type: ignore[arg-type]
            "applied_mode",
            "path_length_mm",
            "commanded_volume_ul",
            "prime_extra_volume_ul",
            "effective_rate_ul_s",
            "rotations",
        }


class TestParseMetadataV2:
    """Schema v2 は未知 key や暗黙の型変換を受理せず、実ファイルと往復できる."""

    @pytest.fixture
    def payload(self) -> dict[str, object]:
        return _load_v2()

    def test_roundtrips_real_v2_file(self, payload: dict[str, object]):
        metadata, error = parse_metadata(payload)

        assert error is None
        assert metadata is not None
        assert _json_roundtrip(metadata) == payload

    def test_cell_and_center_structure_into_geometry_types(
        self, payload: dict[str, object]
    ):
        metadata, _ = parse_metadata(payload)

        assert metadata is not None
        sample = metadata.samples[0]
        assert sample.cell == Rect(8.0, 2.0, 2.0, 2.0)
        assert sample.center == Point2d(9.0, 3.0)
        assert metadata.purge.cell == Rect(2.0, 2.0, 2.0, 2.0)
        assert metadata.purge.center == Point2d(3.0, 3.0)

    def test_sample_carries_commanded_volume_and_volume_index(
        self, payload: dict[str, object]
    ):
        metadata, _ = parse_metadata(payload)

        assert metadata is not None
        sample = metadata.samples[0]
        assert sample.index == 1
        assert sample.commanded_volume_ul == 0.125
        assert sample.volume_index == 2
        assert isinstance(sample.execution, DispenseSummary)
        assert sample.execution.applied_mode == "dot"

    def test_config_exposes_paste_height_crop_size_and_blank_count(
        self, payload: dict[str, object]
    ):
        metadata, _ = parse_metadata(payload)

        assert metadata is not None
        assert metadata.config.paste_height_mm == 0.2
        assert metadata.config.prime_extra_delay_s == 0.0
        assert metadata.config.capture_order == "phased"
        assert metadata.config.crop_size_mm == 2.0
        assert metadata.config.crop_size_px == 241
        assert metadata.config.blank_count == 4
        assert metadata.config.shuffle_seed == 20260908
        assert metadata.plate.width_mm == 40.0
        assert metadata.plate.height_plane_z_mm == 1.62

    def test_label_kind_is_the_rotation_allocated_literal(
        self, payload: dict[str, object]
    ):
        metadata, _ = parse_metadata(payload)

        assert metadata is not None
        assert metadata.label.kind == "rotation_allocated"

    def test_blanks_structure_without_execution_and_with_zero_volume(
        self, payload: dict[str, object]
    ):
        metadata, _ = parse_metadata(payload)

        assert metadata is not None
        blank = metadata.blanks[0]
        assert blank.index == 2
        assert blank.cell == Rect(11.0, 2.0, 2.0, 2.0)
        assert blank.center == Point2d(12.0, 3.0)
        assert blank.measured_volume_ul == 0.0
        assert len(blank.views) == 2

    def test_sample_records_the_dispense_order(self, payload: dict[str, object]):
        metadata, _ = parse_metadata(payload)

        assert metadata is not None
        assert metadata.samples[0].order == 1

    def test_accepts_the_interleaved_capture_order_of_earlier_sessions(
        self, payload: dict[str, object]
    ):
        # 点ごとの interleave で収集済みの dataset を読めなくしない
        config = payload["config"]
        assert isinstance(config, dict)
        config["capture_order"] = "interleaved"

        metadata, error = parse_metadata(payload)

        assert error is None
        assert metadata is not None
        assert metadata.config.capture_order == "interleaved"

    @pytest.mark.parametrize("value", ["batched", "three_pass", "Interleaved", 1])
    def test_rejects_unknown_capture_order(
        self, payload: dict[str, object], value: object
    ):
        config = payload["config"]
        assert isinstance(config, dict)
        config["capture_order"] = value

        metadata, error = parse_metadata(payload)

        assert metadata is None
        assert error is not None

    @pytest.mark.parametrize("value", ["measured", "rotation-allocated", None])
    def test_rejects_unknown_label_kind(
        self, payload: dict[str, object], value: object
    ):
        label = payload["label"]
        assert isinstance(label, dict)
        label["kind"] = value

        metadata, error = parse_metadata(payload)

        assert metadata is None
        assert error is not None

    def test_accepts_missing_manufacturing_lot_and_null_applied_mode(
        self, payload: dict[str, object]
    ):
        paste = payload["paste"]
        purge = payload["purge"]
        assert isinstance(paste, dict) and isinstance(purge, dict)
        paste["lot"] = None
        purge["execution"]["applied_mode"] = None

        metadata, error = parse_metadata(payload)

        assert error is None
        assert metadata is not None
        assert metadata.paste.lot is None
        assert metadata.purge.execution.applied_mode is None
        assert _json_roundtrip(metadata) == payload

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("paste", "lot"), 123),
            (("paste", "density_mg_per_ul"), 3),
            (("camera", "pixel_per_mm"), True),
            (("plate", "width_mm"), 40),
            (("config", "paste_height_mm"), "auto"),
            (("config", "volume_divisions"), 5.0),
            (("config", "samples_per_volume"), "3"),
            (("config", "crop_size_px"), 241.0),
            (("config", "crop_size_mm"), 2),
            (("config", "blank_count"), 4.0),
            (("plate", "height_plane_z_mm"), 2),
            (("samples", 0, "order"), 1.0),
            (("blanks", 0, "measured_volume_ul"), 0),
            (("config", "shuffle_seed"), 20260908.0),
            (("samples", 0, "index"), 1.0),
            (("samples", 0, "volume_index"), 1.0),
            (("samples", 0, "commanded_volume_ul"), 1),
            (("samples", 0, "cell", "width"), 2),
            (("samples", 0, "center", "x"), 9),
            (("samples", 0, "execution", "applied_mode"), "auto"),
        ],
    )
    def test_rejects_implicit_types(
        self, payload: dict[str, object], path: tuple[str | int, ...], value: object
    ):
        target: object = payload
        for key in path[:-1]:
            assert isinstance(target, (dict, list))
            target = target[key]  # type: ignore[index]
        leaf = path[-1]
        assert isinstance(target, dict) and isinstance(leaf, str)
        target[leaf] = value

        metadata, error = parse_metadata(payload)

        assert metadata is None
        assert error is not None
        assert "schema v2" in error

    @pytest.mark.parametrize(
        "section", [None, "paste", "plate", "config", "purge", "label"]
    )
    def test_rejects_unknown_keys(
        self, payload: dict[str, object], section: str | None
    ):
        target = payload
        if section is not None:
            nested = target[section]
            assert isinstance(nested, dict)
            target = nested
        target["unexpected"] = "value"

        metadata, error = parse_metadata(payload)

        assert metadata is None
        assert error is not None
        assert "unexpected" in error

    def test_rejects_reintroduced_mask_path_on_a_view(self, payload: dict[str, object]):
        views = _first_sample(payload)["views"]
        assert isinstance(views, list)
        views[0]["mask"] = "mask/000001.00.png"

        metadata, error = parse_metadata(payload)

        assert metadata is None
        assert error is not None
        assert "mask" in error

    def test_rejects_execution_on_a_blank_entry(self, payload: dict[str, object]):
        blank = _first_blank(payload)
        blank["execution"] = _first_sample(payload)["execution"]

        metadata, error = parse_metadata(payload)

        assert metadata is None
        assert error is not None
        assert "execution" in error

    @pytest.mark.parametrize("version", [0, 1, 3, "2", None])
    def test_rejects_unsupported_schema_version(
        self, payload: dict[str, object], version: object
    ):
        if version is None:
            del payload["schema_version"]
        else:
            payload["schema_version"] = version

        metadata, error = parse_metadata(payload)

        assert metadata is None
        assert error is not None
        assert "schema_version" in error

    def test_rejects_the_real_v1_document_without_migrating_it(self):
        v1_doc = json.loads(METADATA_V1.read_text(encoding="utf-8"))
        assert v1_doc["schema_version"] == 1

        metadata, error = parse_metadata(v1_doc)

        assert metadata is None
        assert error is not None
        assert "schema_version" in error
