"""殟KiCad footprint catalogの公開振る舞いテスト."""

from pathlib import Path

import pytest

from pcbasm.pasting.paste_flow_calibration_board.config import (
    PasteFlowCalibrationBoardConfig,
    PasteFlowCalibrationBoardConfigError,
    PasteFlowCalibrationBoardEnvironmentError,
    PasteFlowCalibrationPattern,
)
from pcbasm.pasting.paste_flow_calibration_board.generator import (
    PasteFlowCalibrationBoardGenerator,
)
from tests.helpers import make_paste_flow_calibration_offset_pad_root
from tests.pcbasm.pasting.paste_flow_calibration_board.support import (
    QFN,
    R0402,
    SOT223,
)

_NAME_MAX_LIBRARY = f"{'l' * 248}.pretty"
_FILESYSTEM_UNSAFE_FOOTPRINT_IDS = [
    pytest.param(f"{'l' * 249}.pretty/Part", id="library-over-name-max"),
    pytest.param(
        f"Test.pretty/{'p' * 246}",
        id="footprint-with-suffix-over-name-max",
    ),
    pytest.param("Test.pretty/Bad\x00Name", id="nul-in-footprint-name"),
]


class TestPasteFlowCalibrationPadCatalog:
    """実ファイル検索と回転同値なパッド形状分類."""

    def test_empty_search_lists_the_fixture_common_footprints(self, generator):
        results = generator.search_footprints("", limit=100)
        footprint_ids = {item.footprint_id for item in results}

        assert len(results) == generator.footprint_count == 8
        assert {
            "Resistor_SMD.pretty/R_0402_1005Metric",
            "Resistor_SMD.pretty/R_0603_1608Metric",
            "Resistor_SMD.pretty/R_0805_2012Metric",
            "Resistor_SMD.pretty/R_1206_3216Metric",
            "Package_TO_SOT_SMD.pretty/SOT-23",
            "Package_TO_SOT_SMD.pretty/SOT-23-5",
            QFN,
            SOT223,
        } == footprint_ids

    def test_searches_the_fixture_library_by_name_tokens(self, generator):
        results = generator.search_footprints("QFN 3x3", limit=10)

        assert [item.footprint_id for item in results] == [QFN]

    def test_collapses_identical_resistor_pads(self, generator):
        patterns = generator.pad_patterns_for("Resistor_SMD.pretty/R_0402_1005Metric")

        assert len(patterns) == 1
        assert patterns[0].catalog_id == R0402
        assert patterns[0].source_pad_numbers == ("1", "2")
        assert patterns[0].source_pad_count == 2
        assert patterns[0].label.startswith("Pad 1–2 ×2")

    def test_separates_qfn_paste_lead_and_exposed_pad_groups(self, generator):
        patterns = generator.pad_patterns_for(QFN)

        assert [item.source_pad_count for item in patterns] == [4, 16, 1]
        assert patterns[0].label.startswith("Paste aperture ×4")
        assert patterns[1].label.startswith("Pad 1–16 ×16")
        assert patterns[2].label.startswith("Pad 17")
        assert len({(item.pad_width_mm, item.pad_height_mm) for item in patterns}) == 3

    def test_separates_sot223_leads_from_the_tab(self, generator):
        patterns = generator.pad_patterns_for(SOT223)

        assert [item.source_pad_count for item in patterns] == [3, 1]
        assert patterns[0].label.startswith("Pad 1–3 ×3")
        assert patterns[1].label.startswith("Pad 2")

    def test_missing_footprint_root_is_an_environment_error(self, tmp_path: Path):
        generator = PasteFlowCalibrationBoardGenerator(tmp_path / "missing")

        with pytest.raises(PasteFlowCalibrationBoardEnvironmentError):
            generator.search_footprints("0402")

    def test_unreadable_footprint_root_is_an_environment_error(self, tmp_path: Path):
        generator = PasteFlowCalibrationBoardGenerator(tmp_path / ("x" * 5_000))

        with pytest.raises(PasteFlowCalibrationBoardEnvironmentError):
            generator.search_footprints("0402")

    def test_wrapped_pad_bounds_are_an_environment_error(self, tmp_path: Path):
        root = make_paste_flow_calibration_offset_pad_root(
            tmp_path / "footprints",
            pad_size_mm=2_000.0,
            shape_offset_x_mm=1_200.0,
        )
        generator = PasteFlowCalibrationBoardGenerator(root)

        with pytest.raises(PasteFlowCalibrationBoardEnvironmentError, match="座標範囲"):
            generator.pad_patterns_for("Test.pretty/OffsetPad")

    def test_library_component_at_name_max_is_not_a_config_error(self, generator):
        assert len(_NAME_MAX_LIBRARY.encode("utf-8")) == 255

        with pytest.raises(PasteFlowCalibrationBoardEnvironmentError):
            generator.pad_patterns_for(f"{_NAME_MAX_LIBRARY}/Part")

    @pytest.mark.parametrize("footprint_id", _FILESYSTEM_UNSAFE_FOOTPRINT_IDS)
    def test_filesystem_unsafe_footprint_ids_are_config_errors(
        self,
        generator,
        footprint_id: str,
    ):
        with pytest.raises(PasteFlowCalibrationBoardConfigError):
            generator.pad_patterns_for(footprint_id)

    @pytest.mark.parametrize("footprint_id", _FILESYSTEM_UNSAFE_FOOTPRINT_IDS)
    def test_filesystem_unsafe_catalog_ids_are_config_errors(
        self,
        generator,
        footprint_id: str,
    ):
        config = PasteFlowCalibrationBoardConfig(
            patterns=(PasteFlowCalibrationPattern(f"{footprint_id}#pad-0"),)
        )

        with pytest.raises(PasteFlowCalibrationBoardConfigError):
            generator.resolve_config(config)
