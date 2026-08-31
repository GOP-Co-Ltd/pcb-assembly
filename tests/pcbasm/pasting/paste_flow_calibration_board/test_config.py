"""流量キャリブレーション基板configとschema 1文書のテスト."""

from typing import cast

import pytest

from pcbasm.pasting.paste_flow_calibration_board import (
    PasteFlowCalibrationBoardConfig,
    PasteFlowCalibrationBoardConfigError,
    PasteFlowCalibrationBoardSpec,
    PasteFlowCalibrationCustomPadShapeId,
    PasteFlowCalibrationPattern,
    PasteFlowCalibrationPurgePadSpec,
    normalize_paste_flow_calibration_board_config,
    parse_paste_flow_calibration_board_document,
    paste_flow_calibration_board_document,
    validate_paste_flow_calibration_board_config,
)
from tests.pcbasm.pasting.paste_flow_calibration_board.support import (
    CUSTOM_A,
    R0402,
    R0603,
    custom_pad,
)


class TestPasteFlowCalibrationBoardConfig:
    """Configの有限値・enum・count validation."""

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(
                PasteFlowCalibrationBoardConfig(
                    board=PasteFlowCalibrationBoardSpec(
                        width_mm=3_000.0,
                        edge_margin_mm=0.0,
                    )
                ),
                id="board-outside-signed-32-bit-nm",
            ),
            pytest.param(
                PasteFlowCalibrationBoardConfig(
                    board=PasteFlowCalibrationBoardSpec(
                        width_mm=0.000_000_6,
                        edge_margin_mm=0.0,
                    )
                ),
                id="board-below-one-nm",
            ),
            pytest.param(
                PasteFlowCalibrationBoardConfig(
                    custom_pads=(
                        custom_pad(
                            CUSTOM_A,
                            "outside coordinate range",
                            width_mm=3_000.0,
                        ),
                    ),
                    patterns=(PasteFlowCalibrationPattern(CUSTOM_A),),
                ),
                id="custom-pad-outside-signed-32-bit-nm",
            ),
            pytest.param(
                PasteFlowCalibrationBoardConfig(
                    custom_pads=(
                        custom_pad(
                            CUSTOM_A,
                            "below one nanometre",
                            width_mm=0.000_000_6,
                        ),
                    ),
                    patterns=(PasteFlowCalibrationPattern(CUSTOM_A),),
                ),
                id="custom-pad-below-one-nm",
            ),
            pytest.param(
                PasteFlowCalibrationBoardConfig(
                    custom_pads=(
                        custom_pad(
                            CUSTOM_A,
                            "maximum vector is unsafe for pad size",
                            width_mm=2_147.483_647,
                        ),
                    ),
                    patterns=(PasteFlowCalibrationPattern(CUSTOM_A),),
                ),
                id="custom-pad-at-signed-32-bit-nm-max",
            ),
            pytest.param(
                PasteFlowCalibrationBoardConfig(
                    purge_pad=PasteFlowCalibrationPurgePadSpec(width_mm=2_147.483_647)
                ),
                id="purge-pad-at-signed-32-bit-nm-max",
            ),
        ],
    )
    def test_kicad_unrepresentable_dimensions_are_config_errors(self, config):
        assert validate_paste_flow_calibration_board_config(config) is not None

        with pytest.raises(PasteFlowCalibrationBoardConfigError):
            normalize_paste_flow_calibration_board_config(config)

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(
                PasteFlowCalibrationBoardConfig(
                    board=PasteFlowCalibrationBoardSpec(
                        width_mm=2_147.483_647,
                        edge_margin_mm=0.0,
                    )
                ),
                id="board-at-signed-32-bit-nm-max",
            ),
            pytest.param(
                PasteFlowCalibrationBoardConfig(
                    custom_pads=(
                        custom_pad(
                            CUSTOM_A,
                            "one nanometre below vector maximum",
                            width_mm=2_147.483_646,
                        ),
                    ),
                    patterns=(PasteFlowCalibrationPattern(CUSTOM_A),),
                ),
                id="custom-pad-one-nm-below-vector-max",
            ),
            pytest.param(
                PasteFlowCalibrationBoardConfig(
                    purge_pad=PasteFlowCalibrationPurgePadSpec(width_mm=2_147.483_646)
                ),
                id="purge-pad-one-nm-below-vector-max",
            ),
        ],
    )
    def test_kicad_boundary_dimensions_are_valid(self, config):
        assert validate_paste_flow_calibration_board_config(config) is None
        assert normalize_paste_flow_calibration_board_config(config) == config

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
                patterns=(PasteFlowCalibrationPattern(R0402, rotation_span_deg=0.0),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern(R0402, rotation_span_deg=361.0),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern(R0402, rotation_count=0),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern(R0402, repeat_count=0),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(
                        R0402,
                        rotation_count=10**400,
                    ),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(
                        R0402,
                        rotation_count=1,
                        repeat_count=5_001,
                    ),
                    PasteFlowCalibrationPattern(
                        R0603,
                        rotation_count=1,
                        repeat_count=5_001,
                    ),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                custom_pads=(
                    custom_pad(
                        CUSTOM_A,
                        "triangle",
                        shape=cast(
                            PasteFlowCalibrationCustomPadShapeId,
                            "triangle",
                        ),
                    ),
                ),
                patterns=(PasteFlowCalibrationPattern(CUSTOM_A),),
            ),
            PasteFlowCalibrationBoardConfig(
                custom_pads=(
                    custom_pad(
                        CUSTOM_A,
                        "bad radius",
                        shape="roundrect",
                        width_mm=1.0,
                        height_mm=0.5,
                        corner_radius_mm=0.3,
                    ),
                ),
                patterns=(PasteFlowCalibrationPattern(CUSTOM_A),),
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern(CUSTOM_A),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(PasteFlowCalibrationPattern("unknown"),)
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(R0402),
                    PasteFlowCalibrationPattern(R0402),
                )
            ),
        ],
    )
    def test_invalid_config_is_rejected_by_public_validation(self, config):
        assert validate_paste_flow_calibration_board_config(config) is not None

        with pytest.raises(PasteFlowCalibrationBoardConfigError):
            normalize_paste_flow_calibration_board_config(config)

    def test_exactly_ten_thousand_generated_pads_is_valid(self):
        config = PasteFlowCalibrationBoardConfig(
            patterns=(
                PasteFlowCalibrationPattern(
                    R0402,
                    rotation_count=100,
                    repeat_count=100,
                ),
            )
        )

        assert validate_paste_flow_calibration_board_config(config) is None

    @pytest.mark.parametrize(
        "config",
        [
            PasteFlowCalibrationBoardConfig(
                auto_pack="yes",  # type: ignore[arg-type]
            ),
            PasteFlowCalibrationBoardConfig(
                board=PasteFlowCalibrationBoardSpec(
                    width_mm=True  # type: ignore[arg-type]
                )
            ),
            PasteFlowCalibrationBoardConfig(
                board=PasteFlowCalibrationBoardSpec(
                    width_mm="40"  # type: ignore[arg-type]
                )
            ),
            PasteFlowCalibrationBoardConfig(
                board=PasteFlowCalibrationBoardSpec(
                    width_mm=10**400  # type: ignore[arg-type]
                )
            ),
            PasteFlowCalibrationBoardConfig(
                custom_pads=(custom_pad(CUSTOM_A, "bool", width_mm=True),),
                patterns=(PasteFlowCalibrationPattern(CUSTOM_A),),
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(
                        R0402,
                        rotation_span_deg=10**400,  # type: ignore[arg-type]
                    ),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(
                        R0402,
                        rotation_count=1.5,  # type: ignore[arg-type]
                    ),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(
                        R0402,
                        rotation_count=True,  # type: ignore[arg-type]
                    ),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(
                        R0402,
                        repeat_count=False,  # type: ignore[arg-type]
                    ),
                )
            ),
            PasteFlowCalibrationBoardConfig(
                patterns=(
                    PasteFlowCalibrationPattern(
                        R0402,
                        transpose="yes",  # type: ignore[arg-type]
                    ),
                )
            ),
        ],
    )
    def test_invalid_runtime_values_do_not_leak_exceptions(self, config):
        assert validate_paste_flow_calibration_board_config(config) is not None

        with pytest.raises(PasteFlowCalibrationBoardConfigError):
            normalize_paste_flow_calibration_board_config(config)


class TestPasteFlowCalibrationBoardDocument:
    """自己識別付きschema 1 JSON文書の公開parse契約."""

    def test_round_trip_preserves_the_normalized_config(self):
        config = PasteFlowCalibrationBoardConfig(
            auto_pack=False,
            board=PasteFlowCalibrationBoardSpec(pad_gap_mm=1.5),
            custom_pads=(custom_pad(CUSTOM_A, "Custom oval", "oval", 1.5, 0.5),),
            patterns=(
                PasteFlowCalibrationPattern(R0603, 360.0, 8, 2, transpose=True),
                PasteFlowCalibrationPattern(R0402, 180.0, 4, 3),
                PasteFlowCalibrationPattern(CUSTOM_A, 180.0, 2, 2),
            ),
        )

        document = paste_flow_calibration_board_document(config)
        restored = parse_paste_flow_calibration_board_document(document)

        assert document["kind"] == "paste_flow_calibration_board"
        assert document["schema_version"] == 1
        assert document["auto_pack"] is False
        assert document["board"]["pad_gap_mm"] == 1.5
        assert document["custom_pads"][0]["shape"] == "oval"
        assert [item["catalog_id"] for item in document["patterns"]] == [
            R0402,
            R0603,
            CUSTOM_A,
        ]
        assert restored == normalize_paste_flow_calibration_board_config(config)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("kind", "calibration_board"),
            ("kind", True),
            ("schema_version", 0),
            ("schema_version", 2),
            ("schema_version", "1"),
            ("schema_version", True),
        ],
    )
    def test_rejects_wrong_document_identity(self, key: str, value: object):
        document = paste_flow_calibration_board_document(
            PasteFlowCalibrationBoardConfig()
        )
        document[key] = value

        assert parse_paste_flow_calibration_board_document(document) is None

    def test_rejects_extra_top_level_or_nested_fields(self):
        top_level = paste_flow_calibration_board_document(
            PasteFlowCalibrationBoardConfig()
        )
        top_level["unexpected"] = True
        nested = paste_flow_calibration_board_document(
            PasteFlowCalibrationBoardConfig()
        )
        nested["board"]["unexpected"] = True

        assert parse_paste_flow_calibration_board_document(top_level) is None
        assert parse_paste_flow_calibration_board_document(nested) is None

    @pytest.mark.parametrize(
        "document",
        [
            None,
            [],
            "not-an-object",
            {},
            {"kind": "paste_flow_calibration_board", "schema_version": 1},
        ],
    )
    def test_malformed_documents_return_none_without_an_exception(self, document):
        assert (
            parse_paste_flow_calibration_board_document(  # type: ignore[arg-type]
                document
            )
            is None
        )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("patterns", "not-a-list"),
            ("custom_pads", {}),
            ("auto_pack", 1),
        ],
    )
    def test_rejects_malformed_config_fields(self, key: str, value: object):
        document = paste_flow_calibration_board_document(
            PasteFlowCalibrationBoardConfig()
        )
        document[key] = value

        assert parse_paste_flow_calibration_board_document(document) is None
