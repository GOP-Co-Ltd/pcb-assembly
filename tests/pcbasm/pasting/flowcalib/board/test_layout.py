"""流量キャリブレーション基板layoutのテスト."""

import pytest

from pcbasm.pasting.flowcalib.board.config import (
    BoardConfig,
    BoardConfigError,
    BoardSpec,
    PatternSpec,
)
from pcbasm.pasting.flowcalib.board.generator import BoardGenerator
from pcbasm.pasting.flowcalib.board.layout import BoardLayout
from tests.pcbasm.pasting.flowcalib.board.support import (
    CUSTOM_A,
    CUSTOM_B,
    R0402,
    R1206,
    custom_pad,
)


def _separated(first, second, gap: float) -> bool:
    return (
        first.x + first.width + gap <= second.x + 1e-9
        or second.x + second.width + gap <= first.x + 1e-9
        or first.y + first.height + gap <= second.y + 1e-9
        or second.y + second.height + gap <= first.y + 1e-9
    )


def _layout(generator: BoardGenerator, config: BoardConfig) -> BoardLayout:
    layout, overflow_message = generator.layout(config)
    assert overflow_message is None
    assert layout is not None
    return layout


class TestBoardLayout:
    """単一パッドごとの回転・繰り返しと自動最適配置."""

    def test_default_recipe_fits_every_pad_without_overlap(self, generator):
        layout = _layout(generator, BoardConfig())

        assert layout.board.width_mm == 40.0
        assert layout.board.height_mm == 40.0
        assert layout.purge_pad.x == 1.0
        assert layout.purge_pad.y == 1.0
        assert layout.purge_pad.width == 2.0
        assert layout.purge_pad.height == 2.0
        assert len(layout.patterns) == 6
        assert layout.pad_count == 64
        assert all(len(pad.polygons) >= 2 for pad in layout.pads)

        purge = layout.purge_pad
        for index, first in enumerate(layout.pads):
            assert first.bounds.x >= 1.0
            assert first.bounds.y >= 1.0
            assert first.bounds.x + first.bounds.width <= 39.0 + 1e-9
            assert first.bounds.y + first.bounds.height <= 39.0 + 1e-9
            assert _separated(first.bounds, purge, 1.0)
            assert all(
                _separated(first.bounds, second.bounds, 1.0)
                for second in layout.pads[index + 1 :]
            )

    def test_purge_pad_only_blocks_its_upper_left_corner(self, generator):
        config = BoardConfig(
            board=BoardSpec(width_mm=12.0, height_mm=8.0),
            custom_pads=(
                custom_pad(CUSTOM_A, "A Right", width_mm=7.0, height_mm=2.0),
                custom_pad(CUSTOM_B, "B Below", width_mm=10.0, height_mm=2.0),
            ),
            patterns=(
                PatternSpec(CUSTOM_A, 180.0, 1, 1),
                PatternSpec(CUSTOM_B, 180.0, 1, 1),
            ),
        )

        pads = {pad.catalog_id: pad for pad in _layout(generator, config).pads}

        assert pads[CUSTOM_A].bounds.x == pytest.approx(4.0)
        assert pads[CUSTOM_A].bounds.y == pytest.approx(1.0)
        assert pads[CUSTOM_B].bounds.x == pytest.approx(1.0)
        assert pads[CUSTOM_B].bounds.y == pytest.approx(4.0)

    def test_resolves_every_rotation_and_repeat_without_grouping(self, generator):
        config = BoardConfig(
            custom_pads=(custom_pad(CUSTOM_A, "Rect", width_mm=4.0, height_mm=1.0),),
            patterns=(PatternSpec(CUSTOM_A, 360.0, 4, 2),),
        )

        layout = _layout(generator, config)

        assert layout.patterns[0].angles_deg == (0.0, 90.0, 180.0, 270.0)
        assert [pad.rotation_deg for pad in layout.pads] == [
            0.0,
            90.0,
            180.0,
            270.0,
            0.0,
            90.0,
            180.0,
            270.0,
        ]
        assert [(pad.bounds.width, pad.bounds.height) for pad in layout.pads[:2]] == [
            pytest.approx((4.0, 1.0)),
            pytest.approx((1.0, 4.0)),
        ]

    def test_independent_pads_fit_when_one_group_rectangle_would_not(self, generator):
        config = BoardConfig(
            board=BoardSpec(
                width_mm=12.0,
                height_mm=8.0,
                pad_gap_mm=1.0,
            ),
            custom_pads=(custom_pad(CUSTOM_A, "Rect", width_mm=4.0, height_mm=1.0),),
            patterns=(PatternSpec(CUSTOM_A, 180.0, 2, 2),),
        )

        layout = _layout(generator, config)

        assert layout.pad_count == 4
        assert all(
            pad.bounds.x + pad.bounds.width <= 11.0 + 1e-9
            and pad.bounds.y + pad.bounds.height <= 7.0 + 1e-9
            for pad in layout.pads
        )

    def test_configured_gap_applies_between_independently_packed_pads(self, generator):
        config = BoardConfig(
            board=BoardSpec(pad_gap_mm=1.5),
            patterns=(PatternSpec(R0402, 180.0, 3, 2),),
        )

        layout = _layout(generator, config)

        for index, first in enumerate(layout.pads):
            assert all(
                _separated(first.bounds, second.bounds, 1.5)
                for second in layout.pads[index + 1 :]
            )

    def test_overflow_reports_the_pad_pattern(self, generator):
        config = BoardConfig(
            board=BoardSpec(width_mm=4.0, height_mm=4.0),
            patterns=(PatternSpec(R1206),),
        )

        layout, overflow_message = generator.layout(config)

        assert layout is None
        assert overflow_message is not None
        assert "1206" in overflow_message

    def test_unknown_pad_variant_is_rejected_after_loading_the_footprint(
        self, generator
    ):
        config = BoardConfig(
            patterns=(PatternSpec("Resistor_SMD.pretty/R_0402_1005Metric#pad-99"),)
        )

        with pytest.raises(BoardConfigError) as exc:
            generator.resolve_config(config)

        assert "指定のパッドパターン" in str(exc.value)
