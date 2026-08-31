"""はんだペースト流量キャリブレーション基板生成のテスト."""

from pathlib import Path

import pcbnew
import pytest

from pcbasm.pasting.paste_flow_calibration_board import (
    PASTE_FLOW_CALIBRATION_BOARD_KIND,
    PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
    PasteFlowCalibrationBoardConfig,
    PasteFlowCalibrationBoardConfigError,
    PasteFlowCalibrationBoardEnvironmentError,
    PasteFlowCalibrationBoardGenerator,
    PasteFlowCalibrationBoardOverflowError,
    PasteFlowCalibrationBoardSpec,
    PasteFlowCalibrationPattern,
    PasteFlowCalibrationPurgePadSpec,
    normalize_paste_flow_calibration_board_config,
    parse_paste_flow_calibration_board_document,
    paste_flow_calibration_board_document,
    validate_paste_flow_calibration_board_config,
)
from pcbasm.pcb import PcbFile
from pcbasm.pcb.generate import save_board

_R0402 = "Resistor_SMD.pretty/R_0402_1005Metric#pad-0"
_R0603 = "Resistor_SMD.pretty/R_0603_1608Metric#pad-0"
_R1206 = "Resistor_SMD.pretty/R_1206_3216Metric#pad-0"
_QFN = "Package_DFN_QFN.pretty/QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
_SOT223 = "Package_TO_SOT_SMD.pretty/SOT-223-3_TabPin2"


class TestPasteFlowCalibrationPadCatalog:
    """実KiCadライブラリの検索とパッド形状分類."""

    def test_lists_all_common_smd_footprints_for_an_empty_search(self):
        results = PasteFlowCalibrationBoardGenerator().search_footprints("", limit=100)

        assert len(results) == 69
        footprint_ids = {item.footprint_id for item in results}
        assert {
            "Resistor_SMD.pretty/R_0201_0603Metric",
            "Capacitor_SMD.pretty/C_1812_4532Metric",
            "Diode_SMD.pretty/D_SMC",
            "Package_TO_SOT_SMD.pretty/TO-263-3_TabPin2",
            "Package_SO.pretty/TSSOP-28_4.4x9.7mm_P0.65mm",
            _QFN,
            "Package_QFP.pretty/LQFP-100_14x14mm_P0.5mm",
            "Crystal.pretty/Crystal_SMD_5032-4Pin_5.0x3.2mm",
        } <= footprint_ids

    def test_searches_the_installed_kicad_footprint_library(self):
        generator = PasteFlowCalibrationBoardGenerator()

        results = generator.search_footprints(
            "QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm", limit=10
        )

        assert generator.footprint_count > 10_000
        assert any(item.footprint_id == _QFN for item in results)

    def test_collapses_the_two_0402_pads_into_one_pad_pattern(self):
        patterns = PasteFlowCalibrationBoardGenerator().pad_patterns_for(
            "Resistor_SMD.pretty/R_0402_1005Metric"
        )

        assert len(patterns) == 1
        assert patterns[0].catalog_id == _R0402
        assert patterns[0].source_pad_numbers == ("1", "2")
        assert patterns[0].source_pad_count == 2
        assert patterns[0].label.startswith("Pad 1–2 ×2")

    def test_separates_qfn_paste_apertures_leads_and_exposed_pad(self):
        patterns = PasteFlowCalibrationBoardGenerator().pad_patterns_for(_QFN)

        assert len(patterns) == 3
        assert [item.source_pad_count for item in patterns] == [4, 16, 1]
        assert patterns[0].label.startswith("Paste aperture ×4")
        assert patterns[1].label.startswith("Pad 1–16 ×16")
        assert patterns[2].label.startswith("Pad 17")
        assert patterns[1].pad_width_mm != patterns[2].pad_width_mm

    def test_separates_sot223_leads_from_the_tab(self):
        patterns = PasteFlowCalibrationBoardGenerator().pad_patterns_for(_SOT223)

        assert len(patterns) == 2
        assert [item.source_pad_count for item in patterns] == [3, 1]
        assert patterns[0].label.startswith("Pad 1–3 ×3")
        assert patterns[1].label.startswith("Pad 2")

    def test_missing_footprint_root_is_an_environment_error(self, tmp_path: Path):
        generator = PasteFlowCalibrationBoardGenerator(tmp_path)

        with pytest.raises(PasteFlowCalibrationBoardEnvironmentError):
            generator.search_footprints("0402")


class TestPasteFlowCalibrationBoardLayout:
    """単一パッドの回転列・繰り返し行とshelf packing."""

    @pytest.fixture
    def generator(self) -> PasteFlowCalibrationBoardGenerator:
        return PasteFlowCalibrationBoardGenerator()

    def test_default_recipe_fits_the_40mm_board(self, generator):
        layout = generator.layout(PasteFlowCalibrationBoardConfig())

        assert layout.board.width_mm == 40.0
        assert layout.board.height_mm == 40.0
        assert layout.board.pad_gap_mm == 1.0
        assert layout.purge_pad.x == 1.0
        assert layout.purge_pad.y == 1.0
        assert layout.purge_pad.width == 2.0
        assert layout.purge_pad.height == 2.0
        assert len(layout.groups) == 6
        assert layout.pad_count == 64
        assert all(
            len(pad.polygons) >= 2 for group in layout.groups for pad in group.pads
        )

    def test_rotation_range_360_is_divided_into_columns(self, generator):
        config = PasteFlowCalibrationBoardConfig(
            patterns=(PasteFlowCalibrationPattern(_R0402, 360.0, 4, 2),)
        )

        group = generator.layout(config).groups[0]

        assert group.angles_deg == (0.0, 90.0, 180.0, 270.0)
        assert group.repeat_count == 2
        assert len(group.pads) == 8
        assert [pad.rotation_deg for pad in group.pads] == [
            0.0,
            90.0,
            180.0,
            270.0,
            0.0,
            90.0,
            180.0,
            270.0,
        ]

    def test_each_family_starts_on_a_new_shelf(self, generator):
        layout = generator.layout(PasteFlowCalibrationBoardConfig())
        resistor_groups = layout.groups[:4]
        first_sot = layout.groups[4]

        assert first_sot.bounds.x == 1.0
        assert first_sot.bounds.y == pytest.approx(
            max(group.bounds.y + group.bounds.height for group in resistor_groups) + 1.0
        )

    def test_pad_and_group_gaps_use_the_configured_minimum(self, generator):
        config = PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(pad_gap_mm=1.5),
            patterns=(
                PasteFlowCalibrationPattern(_R0402, 180.0, 2, 2),
                PasteFlowCalibrationPattern(_R0603, 180.0, 2, 2),
            ),
        )
        layout = generator.layout(config)
        first, second = layout.groups

        assert second.bounds.x - (first.bounds.x + first.bounds.width) == pytest.approx(
            1.5
        )
        assert first.bounds.width == pytest.approx(2 * first.cell_width_mm + 1.5)
        assert first.bounds.height == pytest.approx(2 * first.cell_height_mm + 1.5)

    def test_overflow_reports_the_pad_pattern(self, generator):
        config = PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(width_mm=10.0, height_mm=10.0),
            patterns=(PasteFlowCalibrationPattern(_R1206),),
        )

        with pytest.raises(PasteFlowCalibrationBoardOverflowError) as exc:
            generator.layout(config)

        assert "1206" in str(exc.value)

    def test_unknown_pad_variant_is_rejected_after_loading_the_footprint(
        self, generator
    ):
        config = PasteFlowCalibrationBoardConfig(
            patterns=(
                PasteFlowCalibrationPattern(
                    "Resistor_SMD.pretty/R_0402_1005Metric#pad-99"
                ),
            )
        )

        with pytest.raises(PasteFlowCalibrationBoardConfigError) as exc:
            generator.normalize_config(config)

        assert "指定のパッドパターン" in str(exc.value)


class TestPasteFlowCalibrationBoardConfig:
    """設定検証と自己識別JSONの契約."""

    @pytest.mark.parametrize(
        "config",
        [
            PasteFlowCalibrationBoardConfig(
                board=PasteFlowCalibrationBoardSpec(width_mm=0.0)
            ),
            PasteFlowCalibrationBoardConfig(
                board=PasteFlowCalibrationBoardSpec(edge_margin_mm=-1.0)
            ),
            PasteFlowCalibrationBoardConfig(
                purge_pad=PasteFlowCalibrationPurgePadSpec(width_mm=0.0)
            ),
            PasteFlowCalibrationBoardConfig(patterns=()),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern(_R0402, rotation_span_deg=0.0),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern(_R0402, rotation_span_deg=361.0),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern(_R0402, rotation_count=0),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(
                        _R0402,
                        rotation_count=1.5,  # type: ignore[arg-type]
                    ),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern(_R0402, repeat_count=0),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern("unknown"),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(_R0402),
                    PasteFlowCalibrationPattern(_R0402),
                )
            ),
        ],
    )
    def test_invalid_config_has_a_validation_message(self, config):
        assert validate_paste_flow_calibration_board_config(config) is not None

        with pytest.raises(PasteFlowCalibrationBoardConfigError):
            normalize_paste_flow_calibration_board_config(config)

    def test_document_round_trip_preserves_normalized_pad_config(self):
        config = PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(pad_gap_mm=1.5),
            patterns=(
                PasteFlowCalibrationPattern(_R0603, 360.0, 8, 2),
                PasteFlowCalibrationPattern(_R0402, 180.0, 4, 3),
            ),
        )

        document = paste_flow_calibration_board_document(config)
        restored = parse_paste_flow_calibration_board_document(document)

        assert document["kind"] == PASTE_FLOW_CALIBRATION_BOARD_KIND
        assert document["schema_version"] == PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION
        assert document["board"]["pad_gap_mm"] == 1.5  # type: ignore[index]
        assert restored == normalize_paste_flow_calibration_board_config(config)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("kind", "calibration_board"),
            ("schema_version", 999),
            ("patterns", "not-a-list"),
        ],
    )
    def test_import_rejects_wrong_identity_or_shape(self, key: str, value: object):
        document = paste_flow_calibration_board_document(
            PasteFlowCalibrationBoardConfig()
        )
        document[key] = value

        assert parse_paste_flow_calibration_board_document(document) is None


class TestPasteFlowCalibrationBoardGeneration:
    """生成した実KiCad基板のround-trip."""

    @pytest.fixture
    def board(self) -> pcbnew.BOARD:
        return PasteFlowCalibrationBoardGenerator().build_board(
            PasteFlowCalibrationBoardConfig()
        )

    @pytest.fixture
    def pcb(self, board: pcbnew.BOARD, tmp_path: Path) -> PcbFile:
        output = tmp_path / "paste-flow-calibration.kicad_pcb"
        save_board(board, output)
        return PcbFile(output)

    def test_generated_board_contains_one_pad_per_pattern_instance(self, board):
        footprints = list(board.GetFootprints())

        assert len(footprints) == 65
        assert all(footprint.GetPadCount() == 1 for footprint in footprints)
        assert {footprint.GetReference() for footprint in footprints} >= {
            "PURGE1",
            "PAD1",
            "PAD64",
        }

    def test_qfn_variants_keep_paste_only_lead_and_exposed_pad_layers(self):
        generator = PasteFlowCalibrationBoardGenerator()
        variants = generator.pad_patterns_for(_QFN)
        config = PasteFlowCalibrationBoardConfig(
            patterns=tuple(
                PasteFlowCalibrationPattern(
                    item.catalog_id, rotation_count=1, repeat_count=1
                )
                for item in variants
            )
        )

        board = generator.build_board(config)
        layer_pairs = {
            (
                pad.GetLayerSet().Contains(pcbnew.F_Cu),
                pad.GetLayerSet().Contains(pcbnew.F_Paste),
            )
            for footprint in board.GetFootprints()
            if footprint.GetReference().startswith("PAD")
            for pad in footprint.Pads()
        }

        assert layer_pairs == {(False, True), (True, True), (True, False)}

    def test_generated_board_round_trip_keeps_outline_and_pad_layers(
        self, pcb: PcbFile
    ):
        assert pcb.outline.width == pytest.approx(40.0, abs=0.1)
        assert pcb.outline.height == pytest.approx(40.0, abs=0.1)
        assert len(pcb.components) == 65
        assert len(pcb.pads) == 65
        assert all(pad.polygon.area > 0 for pad in pcb.pads)
        assert all(pad.copper_polygon.area > 0 for pad in pcb.pads)
        purge = [pad for pad in pcb.pads if pad.designator == "PURGE1"]
        assert len(purge) == 1
        assert purge[0].polygon.area == pytest.approx(4.0, abs=0.01)

    def test_rotation_is_written_to_pad_footprints(self, pcb: PcbFile):
        rotations = {
            component.rotation
            for component in pcb.components
            if component.designator.startswith("PAD")
        }

        assert rotations >= {0.0, 45.0, 90.0, 135.0, 180.0, -90.0}

    def test_board_bytes_are_a_kicad_document(self):
        payload = PasteFlowCalibrationBoardGenerator().board_bytes(
            PasteFlowCalibrationBoardConfig()
        )

        assert payload.startswith(b"(kicad_pcb")
