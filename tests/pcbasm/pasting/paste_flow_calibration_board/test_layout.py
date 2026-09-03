"""流量キャリブレーション基板layoutのテスト."""

import pytest

from pcbasm.pasting.paste_flow_calibration_board.config import (
    PasteFlowCalibrationBoardConfig,
    PasteFlowCalibrationBoardConfigError,
    PasteFlowCalibrationBoardOverflowError,
    PasteFlowCalibrationBoardSpec,
    PasteFlowCalibrationPattern,
)
from tests.pcbasm.pasting.paste_flow_calibration_board.support import (
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


class TestPasteFlowCalibrationBoardLayout:
    """単一パッドごとの回転・繰り返しと自動最適配置."""

    def test_default_recipe_fits_every_pad_without_overlap(self, generator):
        layout = generator.layout(PasteFlowCalibrationBoardConfig())

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
        config = PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(width_mm=12.0, height_mm=8.0),
            custom_pads=(
                custom_pad(CUSTOM_A, "A Right", width_mm=7.0, height_mm=2.0),
                custom_pad(CUSTOM_B, "B Below", width_mm=10.0, height_mm=2.0),
            ),
            patterns=(
                PasteFlowCalibrationPattern(CUSTOM_A, 180.0, 1, 1),
                PasteFlowCalibrationPattern(CUSTOM_B, 180.0, 1, 1),
            ),
        )

        pads = {pad.catalog_id: pad for pad in generator.layout(config).pads}

        assert pads[CUSTOM_A].bounds.x == pytest.approx(4.0)
        assert pads[CUSTOM_A].bounds.y == pytest.approx(1.0)
        assert pads[CUSTOM_B].bounds.x == pytest.approx(1.0)
        assert pads[CUSTOM_B].bounds.y == pytest.approx(4.0)

    def test_resolves_every_rotation_and_repeat_without_grouping(self, generator):
        config = PasteFlowCalibrationBoardConfig(
            custom_pads=(custom_pad(CUSTOM_A, "Rect", width_mm=4.0, height_mm=1.0),),
            patterns=(PasteFlowCalibrationPattern(CUSTOM_A, 360.0, 4, 2),),
        )

        layout = generator.layout(config)

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
        config = PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(
                width_mm=12.0,
                height_mm=8.0,
                pad_gap_mm=1.0,
            ),
            custom_pads=(custom_pad(CUSTOM_A, "Rect", width_mm=4.0, height_mm=1.0),),
            patterns=(PasteFlowCalibrationPattern(CUSTOM_A, 180.0, 2, 2),),
        )

        layout = generator.layout(config)

        assert layout.pad_count == 4
        assert all(
            pad.bounds.x + pad.bounds.width <= 11.0 + 1e-9
            and pad.bounds.y + pad.bounds.height <= 7.0 + 1e-9
            for pad in layout.pads
        )

    def test_configured_gap_applies_between_independently_packed_pads(self, generator):
        config = PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(pad_gap_mm=1.5),
            patterns=(PasteFlowCalibrationPattern(R0402, 180.0, 3, 2),),
        )

        layout = generator.layout(config)

        for index, first in enumerate(layout.pads):
            assert all(
                _separated(first.bounds, second.bounds, 1.5)
                for second in layout.pads[index + 1 :]
            )

    def test_overflow_reports_the_pad_pattern(self, generator):
        config = PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(width_mm=4.0, height_mm=4.0),
            patterns=(PasteFlowCalibrationPattern(R1206),),
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
            generator.resolve_config(config)

        assert "指定のパッドパターン" in str(exc.value)
