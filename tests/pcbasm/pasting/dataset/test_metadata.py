"""Dataset metadata schema v1（DTO・strict parse・体積配分・view 検証）の公開契約.

``data/testing/schemas/paste_dataset_metadata_v1.json`` が on-disk 形状のピン。
"""

import json
from pathlib import Path

import pytest

from pcbasm.pasting.applicator import DispenseSummary
from pcbasm.pasting.dataset.metadata import (
    DatasetView,
    PasteDatasetMetadata,
    allocate_volume_by_rotations,
    parse_metadata,
    validate_view,
)
from pcbasm.pasting.params import PasteParams
from tests.helpers import TESTING_DATA_DIR

METADATA_V1 = TESTING_DATA_DIR / "schemas" / "paste_dataset_metadata_v1.json"


def _load_v1() -> dict[str, object]:
    return json.loads(METADATA_V1.read_text(encoding="utf-8"))


def _json_roundtrip(metadata: PasteDatasetMetadata) -> dict[str, object]:
    return json.loads(json.dumps(metadata.to_dict()))


class TestDatasetView:
    """1 pad に対する撮影位置の公開表現と検証."""

    def test_central_view_defaults_to_zero_offsets(self):
        view = DatasetView(number=0)

        assert view.offset_x_mm == 0.0
        assert view.offset_y_mm == 0.0
        assert validate_view(view) is None

    def test_additional_view_keeps_number_and_offsets(self):
        view = DatasetView(number=1, offset_x_mm=2.0, offset_y_mm=-1.5)

        assert validate_view(view) is None
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
        error = validate_view(view)

        assert error is not None
        assert expected in error


class TestAllocateVolumeByRotations:
    """Purge を分母に含めた教師体積の比例配分."""

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


class TestParseMetadata:
    """Schema v1 は未知 key や暗黙の型変換を受理せず、実ファイルと往復できる."""

    @pytest.fixture
    def payload(self) -> dict[str, object]:
        return _load_v1()

    def test_roundtrips_real_v1_file(self, payload: dict[str, object]):
        metadata, error = parse_metadata(payload)

        assert error is None
        assert metadata is not None
        assert _json_roundtrip(metadata) == payload

    def test_resolved_and_execution_use_paste_params_and_dispense_summary(
        self, payload: dict[str, object]
    ):
        metadata, _ = parse_metadata(payload)

        assert metadata is not None
        pad = metadata.pads[0]
        assert isinstance(pad.resolved, PasteParams)
        assert pad.resolved.paste_height == "auto"
        assert isinstance(pad.execution, DispenseSummary)
        assert pad.execution.applied_mode == "mixed"
        assert metadata.purge.execution.applied_mode == "dot"

    def test_resolved_keys_match_paste_params_and_execution_keys_match_summary(
        self, payload: dict[str, object]
    ):
        pads = payload["pads"]
        assert isinstance(pads, list)
        pad = pads[0]
        assert isinstance(pad, dict)

        assert set(pad["resolved"]) == {
            "dispense_mode",
            "line_direction",
            "ul_per_mm2",
            "paste_height",
            "prime_extra_delay",
            "bead_width_factor",
            "overlap",
            "boundary_margin",
        }
        assert set(pad["execution"]) == {
            "applied_mode",
            "path_length_mm",
            "commanded_volume_ul",
            "prime_extra_volume_ul",
            "effective_rate_ul_s",
            "rotations",
        }

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

    def test_accepts_numeric_paste_height(self, payload: dict[str, object]):
        pads = payload["pads"]
        assert isinstance(pads, list)
        pads[0]["resolved"]["paste_height"] = 0.05

        metadata, error = parse_metadata(payload)

        assert error is None
        assert metadata is not None
        assert metadata.pads[0].resolved.paste_height == 0.05

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("paste", "lot"), 123),
            (("paste", "density_mg_per_ul"), 3),
            (("camera", "pixel_per_mm"), True),
            (("pads", 0, "resolved", "paste_height"), 1),
            (("pads", 0, "resolved", "dispense_mode"), "nope"),
            (("pads", 0, "execution", "applied_mode"), "auto"),
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
        assert "schema v1" in error

    @pytest.mark.parametrize("nested", [False, True])
    def test_rejects_unknown_keys(self, payload: dict[str, object], nested: bool):
        target = payload
        if nested:
            paste = target["paste"]
            assert isinstance(paste, dict)
            target = paste
        target["unexpected"] = "value"

        metadata, error = parse_metadata(payload)

        assert metadata is None
        assert error is not None
        assert "unexpected" in error

    @pytest.mark.parametrize("version", [0, 2, "1", None])
    def test_rejects_unknown_schema_version(
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

    def test_fixture_file_is_current_schema(self):
        assert Path(METADATA_V1).is_file()
        payload = _load_v1()
        assert payload["kind"] == "pcbasm-paste-volume-dataset"
        assert payload["schema_version"] == 1
