"""はんだペースト流量キャリブレーション基板の公開仕様テスト."""

from pathlib import Path

import pytest

from pcbasm.pasting.paste_flow_calibration_board import (
    PASTE_FLOW_CALIBRATION_BOARD_KIND,
    PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
    PASTE_FLOW_CALIBRATION_FOOTPRINTS,
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


class TestPasteFlowCalibrationBoardLayout:
    """実KiCad footprintから解決する規則的な配置."""

    @pytest.fixture
    def generator(self) -> PasteFlowCalibrationBoardGenerator:
        return PasteFlowCalibrationBoardGenerator()

    def test_default_recipe_fits_40mm_board(
        self, generator: PasteFlowCalibrationBoardGenerator
    ):
        layout = generator.layout(PasteFlowCalibrationBoardConfig())

        assert layout.board.width_mm == 40.0
        assert layout.board.height_mm == 40.0
        assert layout.purge_pad.x == 1.0
        assert layout.purge_pad.y == 1.0
        assert layout.purge_pad.width == 2.0
        assert layout.purge_pad.height == 2.0
        assert [group.label for group in layout.groups] == [
            "0402",
            "0603",
            "0805",
            "1206",
            "SOT-23",
            "SOT-23-5",
        ]
        assert layout.component_count == 64
        assert (
            max(group.bounds.y + group.bounds.height for group in layout.groups) <= 39.0
        )

    def test_rotation_columns_and_repeat_rows_are_resolved(
        self, generator: PasteFlowCalibrationBoardGenerator
    ):
        config = PasteFlowCalibrationBoardConfig(
            patterns=(
                PasteFlowCalibrationPattern(
                    "r_0402_1005metric",
                    rotation_span_deg=360.0,
                    rotation_count=4,
                    repeat_count=2,
                ),
            )
        )

        group = generator.layout(config).groups[0]

        assert group.angles_deg == (0.0, 90.0, 180.0, 270.0)
        assert group.repeat_count == 2
        assert len(group.components) == 8
        assert [component.rotation_deg for component in group.components] == [
            0.0,
            90.0,
            180.0,
            270.0,
            0.0,
            90.0,
            180.0,
            270.0,
        ]

    def test_groups_keep_gap_and_each_family_starts_a_new_shelf(
        self, generator: PasteFlowCalibrationBoardGenerator
    ):
        layout = generator.layout(PasteFlowCalibrationBoardConfig())
        groups = {group.label: group for group in layout.groups}

        assert groups["0603"].bounds.x >= (
            groups["0402"].bounds.x + groups["0402"].bounds.width + 1.0
        )
        assert groups["0805"].bounds.y >= (
            groups["0402"].bounds.y
            + max(groups["0402"].bounds.height, groups["0603"].bounds.height)
            + 1.0
        )
        assert groups["SOT-23"].bounds.x == 1.0
        assert groups["SOT-23"].bounds.y >= (
            groups["1206"].bounds.y + groups["1206"].bounds.height + 1.0
        )

    def test_server_canonicalizes_catalog_order(
        self, generator: PasteFlowCalibrationBoardGenerator
    ):
        config = PasteFlowCalibrationBoardConfig(
            patterns=(
                PasteFlowCalibrationPattern("sot_23"),
                PasteFlowCalibrationPattern("r_0402_1005metric"),
            )
        )

        layout = generator.layout(config)

        assert [group.catalog_id for group in layout.groups] == [
            "r_0402_1005metric",
            "sot_23",
        ]

    def test_overflow_reports_the_component_that_does_not_fit(
        self, generator: PasteFlowCalibrationBoardGenerator
    ):
        config = PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(width_mm=10.0, height_mm=10.0),
            patterns=(PasteFlowCalibrationPattern("r_1206_3216metric"),),
        )

        with pytest.raises(PasteFlowCalibrationBoardOverflowError) as exc:
            generator.layout(config)

        assert "1206" in str(exc.value)

    def test_missing_footprint_root_is_environment_error(self, tmp_path: Path):
        generator = PasteFlowCalibrationBoardGenerator(tmp_path)

        with pytest.raises(PasteFlowCalibrationBoardEnvironmentError) as exc:
            generator.layout(PasteFlowCalibrationBoardConfig())

        assert "KiCad footprint" in str(exc.value)


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
                patterns=(
                    PasteFlowCalibrationPattern(
                        "r_0402_1005metric", rotation_span_deg=0.0
                    ),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(
                        "r_0402_1005metric", rotation_span_deg=361.0
                    ),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern("r_0402_1005metric", rotation_count=0),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(
                        "r_0402_1005metric",
                        rotation_count=1.5,  # type: ignore[arg-type]
                    ),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern("r_0402_1005metric", repeat_count=0),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern("unknown"),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern("r_0402_1005metric"),
                    PasteFlowCalibrationPattern("r_0402_1005metric"),
                )
            ),
        ],
    )
    def test_invalid_config_has_a_validation_message(
        self, config: PasteFlowCalibrationBoardConfig
    ):
        assert validate_paste_flow_calibration_board_config(config) is not None

        with pytest.raises(PasteFlowCalibrationBoardConfigError):
            normalize_paste_flow_calibration_board_config(config)

    def test_document_round_trip_preserves_normalized_config(self):
        config = PasteFlowCalibrationBoardConfig(
            patterns=(
                PasteFlowCalibrationPattern("sot_23", 360.0, 8, 2),
                PasteFlowCalibrationPattern("r_0402_1005metric", 180.0, 4, 3),
            )
        )

        document = paste_flow_calibration_board_document(config)
        restored = parse_paste_flow_calibration_board_document(document)

        assert document["kind"] == PASTE_FLOW_CALIBRATION_BOARD_KIND
        assert document["schema_version"] == PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION
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

    def test_catalog_contains_the_eleven_practical_footprints(self):
        assert [item.label for item in PASTE_FLOW_CALIBRATION_FOOTPRINTS] == [
            "0402",
            "0603",
            "0805",
            "1206",
            "SOT-23",
            "SOT-23-5",
            "SOIC-8",
            "TSSOP-14",
            "QFN-16 EP",
            "LQFP-32",
            "SOT-223",
        ]


class TestPasteFlowCalibrationBoardGeneration:
    """生成した実KiCad基板のround-trip."""

    @pytest.fixture
    def pcb(self, tmp_path: Path) -> PcbFile:
        generator = PasteFlowCalibrationBoardGenerator()
        board = generator.build_board(PasteFlowCalibrationBoardConfig())
        output = tmp_path / "paste-flow-calibration.kicad_pcb"
        save_board(board, output)
        return PcbFile(output)

    def test_generated_board_has_expected_outline_and_components(self, pcb: PcbFile):
        assert pcb.outline.width == pytest.approx(40.0, abs=0.1)
        assert pcb.outline.height == pytest.approx(40.0, abs=0.1)
        assert len(pcb.components) == 65
        assert {component.designator for component in pcb.components} >= {
            "PURGE1",
            "R1",
            "Q1",
            "U1",
        }

    def test_generated_board_keeps_real_paste_and_copper_shapes(self, pcb: PcbFile):
        assert len(pcb.pads) > 64
        assert all(pad.polygon.area > 0 for pad in pcb.pads)
        assert all(pad.copper_polygon.area > 0 for pad in pcb.pads)
        purge = [pad for pad in pcb.pads if pad.designator == "PURGE1"]
        assert len(purge) == 1
        assert purge[0].polygon.area == pytest.approx(4.0, abs=0.01)

    def test_rotation_is_written_to_footprints(self, pcb: PcbFile):
        rotations = {
            component.rotation
            for component in pcb.components
            if component.designator.startswith("R")
        }

        assert rotations >= {0.0, 45.0, 90.0, 135.0}

    def test_board_bytes_are_a_kicad_document(self):
        payload = PasteFlowCalibrationBoardGenerator().board_bytes(
            PasteFlowCalibrationBoardConfig()
        )

        assert payload.startswith(b"(kicad_pcb")
