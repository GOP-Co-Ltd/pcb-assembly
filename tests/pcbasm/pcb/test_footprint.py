"""pcbasm.pcb.footprint のテスト（実 ``*.pretty`` library と実 pcbnew を使う）."""

from pathlib import Path

import pcbnew
import pytest

from pcbasm.pcb.footprint import (
    FRONT_PAD_LAYERS,
    FootprintEnvelope,
    FootprintLibrary,
    FootprintLibraryError,
    KicadCoordinateError,
    duplicate_footprint,
    footprint_polygons,
    format_footprint_id,
    pad_geometry_signature,
    pad_on_any_layer,
    parse_footprint_id,
    search_tokens,
    single_pad_footprint,
    smd_pad_footprint,
)
from tests.helpers import make_paste_test_board_offset_pad_root


@pytest.fixture
def library(paste_test_board_footprint_root: Path) -> FootprintLibrary:
    return FootprintLibrary(paste_test_board_footprint_root)


class TestFootprintId:
    def test_format_and_parse_round_trip(self):
        footprint_id = format_footprint_id("Resistor_SMD.pretty", "R_0402_1005Metric")
        assert footprint_id == "Resistor_SMD.pretty/R_0402_1005Metric"
        assert parse_footprint_id(footprint_id) == (
            "Resistor_SMD.pretty",
            "R_0402_1005Metric",
        )

    @pytest.mark.parametrize(
        "footprint_id",
        [
            None,
            "no-slash",
            "a/b/c",
            "NotPretty/Part",
            "Test.pretty/../Part",
            "Test.pretty/Bad\x00Name",
            f"{'l' * 249}.pretty/Part",
        ],
    )
    def test_rejects_malformed_or_unsafe_ids(self, footprint_id: object):
        assert parse_footprint_id(footprint_id) is None


class TestSearchTokens:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("", ()),
            ("  _-/ ", ()),
            ("QFN 3x3", ("qfn", "3x3")),
            ("R_0402_1005Metric", ("r", "0402", "1005metric")),
        ],
    )
    def test_splits_and_casefolds(self, query: str, expected: tuple[str, ...]):
        assert search_tokens(query) == expected


class TestFootprintLibrary:
    def test_indexes_fixture_root(self, library: FootprintLibrary):
        ids = {item.footprint_id for item in library.footprints}
        assert len(ids) == 8
        assert "Resistor_SMD.pretty/R_0402_1005Metric" in ids
        assert all(" / " in item.label for item in library.footprints)

    def test_search_ranks_exact_match_first(self, library: FootprintLibrary):
        results = [item.footprint for item in library.search("SOT-23", limit=10)]
        assert results[0] == "SOT-23"
        assert results[1] == "SOT-23-5"
        assert "R_0402_1005Metric" not in results

    def test_search_empty_query_returns_nothing(self, library: FootprintLibrary):
        assert library.search("", limit=10) == ()

    def test_search_respects_limit(self, library: FootprintLibrary):
        assert len(library.search("R", limit=2)) == 2

    def test_load_returns_footprint_with_pads(self, library: FootprintLibrary):
        footprint = library.load("Resistor_SMD.pretty", "R_0402_1005Metric")
        assert len(list(footprint.Pads())) == 2

    def test_missing_root_is_a_library_error(self, tmp_path: Path):
        with pytest.raises(FootprintLibraryError):
            FootprintLibrary(tmp_path / "missing").footprints

    def test_missing_footprint_is_a_library_error(self, library: FootprintLibrary):
        with pytest.raises(FootprintLibraryError):
            library.load("Resistor_SMD.pretty", "Nope")

    def test_env_var_selects_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
        paste_test_board_footprint_root: Path,
    ):
        monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", str(paste_test_board_footprint_root))
        assert FootprintLibrary().root == paste_test_board_footprint_root


class TestPadGeometry:
    def test_single_pad_footprint_moves_pad_to_origin(self, library: FootprintLibrary):
        footprint = library.load("Resistor_SMD.pretty", "R_0402_1005Metric")
        pad = next(iter(footprint.Pads()))
        single = single_pad_footprint(pad)
        pads = list(single.Pads())
        assert len(pads) == 1
        assert pads[0].GetNumber() == "1"
        assert (pads[0].GetPosition().x, pads[0].GetPosition().y) == (0, 0)

    def test_smd_pad_footprint_envelope_matches_size(self):
        footprint = smd_pad_footprint("rectangle", 1.2, 0.8)
        envelope = FootprintEnvelope.measure(footprint, 0.0)
        assert envelope.width == pytest.approx(1.2, abs=1e-6)
        assert envelope.height == pytest.approx(0.8, abs=1e-6)

    def test_envelope_rotates_with_angle(self):
        footprint = smd_pad_footprint("rectangle", 1.2, 0.8)
        envelope = FootprintEnvelope.measure(footprint, 90.0)
        assert envelope.width == pytest.approx(0.8, abs=1e-6)
        assert envelope.height == pytest.approx(1.2, abs=1e-6)

    def test_signature_is_rotation_invariant(self):
        upright = smd_pad_footprint("rectangle", 1.2, 0.8)
        rotated = smd_pad_footprint("rectangle", 0.8, 1.2)
        different = smd_pad_footprint("rectangle", 1.0, 1.0)
        assert pad_geometry_signature(upright) == pad_geometry_signature(rotated)
        assert pad_geometry_signature(upright) != pad_geometry_signature(different)

    def test_footprint_polygons_expose_front_layers(self):
        footprint = smd_pad_footprint("rectangle", 1.0, 1.0)
        polygons = footprint_polygons(
            footprint, (("F.Cu", pcbnew.F_Cu), ("F.Paste", pcbnew.F_Paste))
        )
        assert {name for name, _ in polygons} == {"F.Cu", "F.Paste"}
        assert all(len(points) >= 4 for _, points in polygons)

    def test_pad_on_any_layer(self):
        footprint = smd_pad_footprint("circle", 1.0, 1.0)
        pad = next(iter(footprint.Pads()))
        assert pad_on_any_layer(pad, FRONT_PAD_LAYERS) is True
        assert pad_on_any_layer(pad, (pcbnew.B_Cu,)) is False

    def test_duplicate_footprint_is_independent(self):
        original = smd_pad_footprint("oval", 1.0, 2.0)
        copy = duplicate_footprint(original)
        copy.SetOrientationDegrees(90.0)
        assert original.GetOrientationDegrees() == 0.0

    def test_wrapped_pad_bounds_are_a_coordinate_error(self, tmp_path: Path):
        root = make_paste_test_board_offset_pad_root(
            tmp_path / "footprints", pad_size_mm=2_000.0, shape_offset_x_mm=1_200.0
        )
        footprint = FootprintLibrary(root).load("Test.pretty", "OffsetPad")
        with pytest.raises(KicadCoordinateError, match="座標範囲"):
            FootprintEnvelope.measure(footprint, 0.0)
