"""テスト塗布基板configとschema 1文書のテスト."""

from typing import cast

import pytest

from pcbasm.pasting.testboard.config import (
    BoardConfig,
    BoardConfigError,
    BoardSpec,
    CustomPadShapeId,
    PatternSpec,
    PurgePadSpec,
    parse_board_document,
)
from tests.pcbasm.pasting.testboard.support import (
    CUSTOM_A,
    R0402,
    R0603,
    custom_pad,
)


class TestBoardConfig:
    """Configの有限値・enum・count validation."""

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(
                BoardConfig(
                    board=BoardSpec(
                        width_mm=3_000.0,
                        edge_margin_mm=0.0,
                    )
                ),
                id="board-outside-signed-32-bit-nm",
            ),
            pytest.param(
                BoardConfig(
                    board=BoardSpec(
                        width_mm=0.000_000_6,
                        edge_margin_mm=0.0,
                    )
                ),
                id="board-below-one-nm",
            ),
            pytest.param(
                BoardConfig(
                    custom_pads=(
                        custom_pad(
                            CUSTOM_A,
                            "outside coordinate range",
                            width_mm=3_000.0,
                        ),
                    ),
                    patterns=(PatternSpec(CUSTOM_A),),
                ),
                id="custom-pad-outside-signed-32-bit-nm",
            ),
            pytest.param(
                BoardConfig(
                    custom_pads=(
                        custom_pad(
                            CUSTOM_A,
                            "below one nanometre",
                            width_mm=0.000_000_6,
                        ),
                    ),
                    patterns=(PatternSpec(CUSTOM_A),),
                ),
                id="custom-pad-below-one-nm",
            ),
            pytest.param(
                BoardConfig(
                    custom_pads=(
                        custom_pad(
                            CUSTOM_A,
                            "maximum vector is unsafe for pad size",
                            width_mm=2_147.483_647,
                        ),
                    ),
                    patterns=(PatternSpec(CUSTOM_A),),
                ),
                id="custom-pad-at-signed-32-bit-nm-max",
            ),
            pytest.param(
                BoardConfig(purge_pad=PurgePadSpec(width_mm=2_147.483_647)),
                id="purge-pad-at-signed-32-bit-nm-max",
            ),
        ],
    )
    def test_kicad_unrepresentable_dimensions_are_config_errors(self, config):
        assert config.validate() is not None

        with pytest.raises(BoardConfigError):
            config.normalized()

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(
                BoardConfig(
                    board=BoardSpec(
                        width_mm=2_147.483_647,
                        edge_margin_mm=0.0,
                    )
                ),
                id="board-at-signed-32-bit-nm-max",
            ),
            pytest.param(
                BoardConfig(
                    custom_pads=(
                        custom_pad(
                            CUSTOM_A,
                            "one nanometre below vector maximum",
                            width_mm=2_147.483_646,
                        ),
                    ),
                    patterns=(PatternSpec(CUSTOM_A),),
                ),
                id="custom-pad-one-nm-below-vector-max",
            ),
            pytest.param(
                BoardConfig(purge_pad=PurgePadSpec(width_mm=2_147.483_646)),
                id="purge-pad-one-nm-below-vector-max",
            ),
        ],
    )
    def test_kicad_boundary_dimensions_are_valid(self, config):
        assert config.validate() is None
        assert config.normalized() == config

    @pytest.mark.parametrize(
        "config",
        [
            BoardConfig(board=BoardSpec(width_mm=0.0)),
            BoardConfig(board=BoardSpec(edge_margin_mm=-1.0)),
            BoardConfig(purge_pad=PurgePadSpec(width_mm=0.0)),
            BoardConfig(patterns=()),
            BoardConfig(patterns=(PatternSpec(R0402, rotation_span_deg=0.0),)),
            BoardConfig(patterns=(PatternSpec(R0402, rotation_span_deg=361.0),)),
            BoardConfig(patterns=(PatternSpec(R0402, rotation_count=0),)),
            BoardConfig(patterns=(PatternSpec(R0402, repeat_count=0),)),
            BoardConfig(
                patterns=(
                    PatternSpec(
                        R0402,
                        rotation_count=10**400,
                    ),
                )
            ),
            BoardConfig(
                patterns=(
                    PatternSpec(
                        R0402,
                        rotation_count=1,
                        repeat_count=5_001,
                    ),
                    PatternSpec(
                        R0603,
                        rotation_count=1,
                        repeat_count=5_001,
                    ),
                )
            ),
            BoardConfig(
                custom_pads=(
                    custom_pad(
                        CUSTOM_A,
                        "triangle",
                        shape=cast(
                            CustomPadShapeId,
                            "triangle",
                        ),
                    ),
                ),
                patterns=(PatternSpec(CUSTOM_A),),
            ),
            BoardConfig(
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
                patterns=(PatternSpec(CUSTOM_A),),
            ),
            BoardConfig(patterns=(PatternSpec(CUSTOM_A),)),
            BoardConfig(patterns=(PatternSpec("unknown"),)),
            BoardConfig(
                patterns=(
                    PatternSpec(R0402),
                    PatternSpec(R0402),
                )
            ),
        ],
    )
    def test_invalid_config_is_rejected_by_public_validation(self, config):
        assert config.validate() is not None

        with pytest.raises(BoardConfigError):
            config.normalized()

    def test_exactly_ten_thousand_generated_pads_is_valid(self):
        config = BoardConfig(
            patterns=(
                PatternSpec(
                    R0402,
                    rotation_count=100,
                    repeat_count=100,
                ),
            )
        )

        assert config.validate() is None

    @pytest.mark.parametrize(
        "config",
        [
            BoardConfig(
                board=BoardSpec(
                    width_mm=True  # type: ignore[arg-type]
                )
            ),
            BoardConfig(
                board=BoardSpec(
                    width_mm="40"  # type: ignore[arg-type]
                )
            ),
            BoardConfig(
                board=BoardSpec(
                    width_mm=10**400  # type: ignore[arg-type]
                )
            ),
            BoardConfig(
                custom_pads=(custom_pad(CUSTOM_A, "bool", width_mm=True),),
                patterns=(PatternSpec(CUSTOM_A),),
            ),
            BoardConfig(
                patterns=(
                    PatternSpec(
                        R0402,
                        rotation_span_deg=10**400,  # type: ignore[arg-type]
                    ),
                )
            ),
            BoardConfig(
                patterns=(
                    PatternSpec(
                        R0402,
                        rotation_count=1.5,  # type: ignore[arg-type]
                    ),
                )
            ),
            BoardConfig(
                patterns=(
                    PatternSpec(
                        R0402,
                        rotation_count=True,  # type: ignore[arg-type]
                    ),
                )
            ),
            BoardConfig(
                patterns=(
                    PatternSpec(
                        R0402,
                        repeat_count=False,  # type: ignore[arg-type]
                    ),
                )
            ),
        ],
    )
    def test_invalid_runtime_values_do_not_leak_exceptions(self, config):
        assert config.validate() is not None

        with pytest.raises(BoardConfigError):
            config.normalized()


class TestBoardDocument:
    """自己識別付きschema 1 JSON文書の公開parse契約."""

    def test_round_trip_preserves_the_normalized_config(self):
        config = BoardConfig(
            board=BoardSpec(pad_gap_mm=1.5),
            custom_pads=(custom_pad(CUSTOM_A, "Custom oval", "oval", 1.5, 0.5),),
            patterns=(
                PatternSpec(R0603, 360.0, 8, 2),
                PatternSpec(R0402, 180.0, 4, 3),
                PatternSpec(CUSTOM_A, 180.0, 2, 2),
            ),
        )

        document = config.to_normalized_document()
        restored = parse_board_document(document)

        assert document["kind"] == "paste_test_board"
        assert document["schema_version"] == 1
        assert document["board"]["pad_gap_mm"] == 1.5
        assert document["custom_pads"][0]["shape"] == "oval"
        assert [item["catalog_id"] for item in document["patterns"]] == [
            R0402,
            R0603,
            CUSTOM_A,
        ]
        assert restored == config.normalized()

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
        document = BoardConfig().to_normalized_document()
        document[key] = value

        assert parse_board_document(document) is None

    def test_rejects_extra_top_level_or_nested_fields(self):
        top_level = BoardConfig().to_normalized_document()
        top_level["unexpected"] = True
        nested = BoardConfig().to_normalized_document()
        nested["board"]["unexpected"] = True

        assert parse_board_document(top_level) is None
        assert parse_board_document(nested) is None

    @pytest.mark.parametrize(
        "document",
        [
            None,
            [],
            "not-an-object",
            {},
            {"kind": "paste_test_board", "schema_version": 1},
        ],
    )
    def test_malformed_documents_return_none_without_an_exception(self, document):
        assert (
            parse_board_document(  # type: ignore[arg-type]
                document
            )
            is None
        )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("patterns", "not-a-list"),
            ("custom_pads", {}),
        ],
    )
    def test_rejects_malformed_config_fields(self, key: str, value: object):
        document = BoardConfig().to_normalized_document()
        document[key] = value

        assert parse_board_document(document) is None
