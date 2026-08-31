"""流量キャリブレーション基板layoutのテスト."""

import attrs
import pytest

from pcbasm.pasting.paste_flow_calibration_board import (
    PasteFlowCalibrationBoardConfig,
    PasteFlowCalibrationBoardConfigError,
    PasteFlowCalibrationBoardOverflowError,
    PasteFlowCalibrationBoardSpec,
    PasteFlowCalibrationPattern,
)
from tests.pcbasm.pasting.paste_flow_calibration_board.support import (
    CUSTOM_A,
    CUSTOM_B,
    CUSTOM_C,
    R0402,
    R0603,
    R1206,
    custom_pad,
)


class TestPasteFlowCalibrationBoardLayout:
    """単一パッドの回転・繰り返し配置とpacking."""

    def test_default_recipe_fits_the_board_without_overlap(self, generator):
        layout = generator.layout(PasteFlowCalibrationBoardConfig())

        assert layout.board.width_mm == 40.0
        assert layout.board.height_mm == 40.0
        assert layout.purge_pad.x == 1.0
        assert layout.purge_pad.y == 1.0
        assert layout.purge_pad.width == 2.0
        assert layout.purge_pad.height == 2.0
        assert len(layout.groups) == 6
        assert layout.pad_count == 64
        assert all(
            len(pad.polygons) >= 2 for group in layout.groups for pad in group.pads
        )
        purge_right = layout.purge_pad.x + layout.purge_pad.width
        purge_bottom = layout.purge_pad.y + layout.purge_pad.height
        for index, first in enumerate(layout.groups):
            assert first.bounds.x >= 1.0
            assert first.bounds.y >= 1.0
            assert first.bounds.x + first.bounds.width <= 39.0 + 1e-9
            assert first.bounds.y + first.bounds.height <= 39.0 + 1e-9
            assert (
                purge_right + 1.0 <= first.bounds.x + 1e-9
                or purge_bottom + 1.0 <= first.bounds.y + 1e-9
            )
            for second in layout.groups[index + 1 :]:
                assert (
                    first.bounds.x + first.bounds.width + 1.0 <= second.bounds.x + 1e-9
                    or second.bounds.x + second.bounds.width + 1.0
                    <= first.bounds.x + 1e-9
                    or first.bounds.y + first.bounds.height + 1.0
                    <= second.bounds.y + 1e-9
                    or second.bounds.y + second.bounds.height + 1.0
                    <= first.bounds.y + 1e-9
                )

    @pytest.mark.parametrize("auto_pack", [False, True])
    def test_purge_pad_only_blocks_its_upper_left_corner(self, generator, auto_pack):
        config = PasteFlowCalibrationBoardConfig(
            auto_pack=auto_pack,
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

        groups = {group.catalog_id: group for group in generator.layout(config).groups}

        assert groups[CUSTOM_A].bounds.x == pytest.approx(4.0)
        assert groups[CUSTOM_A].bounds.y == pytest.approx(1.0)
        assert groups[CUSTOM_B].bounds.x == pytest.approx(1.0)
        assert groups[CUSTOM_B].bounds.y == pytest.approx(4.0)

    def test_transpose_places_repeats_in_columns_and_rotations_in_rows(self, generator):
        config = PasteFlowCalibrationBoardConfig(
            auto_pack=False,
            patterns=(PasteFlowCalibrationPattern(R0402, 360.0, 4, 2, transpose=True),),
        )

        group = generator.layout(config).groups[0]

        assert group.angles_deg == (0.0, 90.0, 180.0, 270.0)
        assert group.repeat_count == 2
        assert group.transpose is True
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
        assert group.pads[0].x == pytest.approx(group.pads[1].x)
        assert group.pads[0].y != pytest.approx(group.pads[1].y)
        assert group.pads[0].x != pytest.approx(group.pads[4].x)
        assert group.pads[0].y == pytest.approx(group.pads[4].y)

    def test_non_transposed_placement_puts_rotations_in_columns(self, generator):
        config = PasteFlowCalibrationBoardConfig(
            auto_pack=False,
            patterns=(
                PasteFlowCalibrationPattern(R0402, 360.0, 4, 2, transpose=False),
            ),
        )

        group = generator.layout(config).groups[0]

        assert group.transpose is False
        assert group.pads[0].x != pytest.approx(group.pads[1].x)
        assert group.pads[0].y == pytest.approx(group.pads[1].y)
        assert group.pads[0].x == pytest.approx(group.pads[4].x)
        assert group.pads[0].y != pytest.approx(group.pads[4].y)

    def test_each_footprint_family_starts_on_a_new_shelf(self, generator):
        layout = generator.layout(PasteFlowCalibrationBoardConfig(auto_pack=False))
        resistor_groups = layout.groups[:4]
        first_sot = layout.groups[4]

        assert first_sot.bounds.x == 1.0
        assert first_sot.bounds.y == pytest.approx(
            max(group.bounds.y + group.bounds.height for group in resistor_groups) + 1.0
        )

    def test_pad_and_group_gaps_use_the_configured_minimum(self, generator):
        config = PasteFlowCalibrationBoardConfig(
            auto_pack=False,
            board=PasteFlowCalibrationBoardSpec(pad_gap_mm=1.5),
            patterns=(
                PasteFlowCalibrationPattern(R0402, 180.0, 3, 2),
                PasteFlowCalibrationPattern(R0603, 180.0, 3, 2),
            ),
        )
        layout = generator.layout(config)
        first, second = layout.groups

        assert second.bounds.x - (first.bounds.x + first.bounds.width) == (
            pytest.approx(1.5)
        )
        assert first.bounds.width == pytest.approx(3 * first.cell_width_mm + 3.0)
        assert first.bounds.height == pytest.approx(2 * first.cell_height_mm + 1.5)

    def test_auto_pack_transposes_a_group_when_only_that_orientation_fits(
        self, generator
    ):
        config = PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(width_mm=8.0, height_mm=15.0),
            custom_pads=(custom_pad(CUSTOM_A, "Circle", "circle", 1.0, 1.0),),
            patterns=(PasteFlowCalibrationPattern(CUSTOM_A, 180.0, 4, 2),),
        )

        group = generator.layout(config).groups[0]

        assert group.transpose is True
        assert group.bounds.width == pytest.approx(2 * group.cell_width_mm + 1.0)
        assert group.bounds.height == pytest.approx(4 * group.cell_height_mm + 3.0)

    def test_auto_pack_backfills_space_that_ordered_shelves_leave_unused(
        self, generator
    ):
        config = PasteFlowCalibrationBoardConfig(
            auto_pack=False,
            board=PasteFlowCalibrationBoardSpec(
                width_mm=12.0,
                height_mm=13.0,
                pad_gap_mm=0.0,
            ),
            custom_pads=(
                custom_pad(CUSTOM_A, "A", "circle"),
                custom_pad(CUSTOM_B, "B", "circle"),
                custom_pad(CUSTOM_C, "C", "circle"),
            ),
            patterns=(
                PasteFlowCalibrationPattern(CUSTOM_A, 180.0, 6, 6),
                PasteFlowCalibrationPattern(CUSTOM_B, 180.0, 4, 4),
                PasteFlowCalibrationPattern(CUSTOM_C, 180.0, 4, 6),
            ),
        )

        with pytest.raises(PasteFlowCalibrationBoardOverflowError):
            generator.layout(config)

        layout = generator.layout(attrs.evolve(config, auto_pack=True))

        assert len(layout.groups) == 3
        assert (
            max(group.bounds.y + group.bounds.height for group in layout.groups)
            <= 12.0 + 1e-9
        )

    def test_overflow_reports_the_pad_pattern(self, generator):
        config = PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(width_mm=10.0, height_mm=10.0),
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
