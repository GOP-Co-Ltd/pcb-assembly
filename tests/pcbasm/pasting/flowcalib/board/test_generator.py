"""流量キャリブレーション基板Generator facadeのテスト."""

import threading
from pathlib import Path
from time import monotonic

import pcbnew
import pytest

from pcbasm.pasting.flowcalib.board.config import (
    BoardConfig,
    BoardSpec,
    CustomPadDraft,
    PatternSpec,
    PurgePadSpec,
)
from pcbasm.pasting.flowcalib.board.generator import (
    BoardGenerator,
)
from pcbasm.pcb import PcbFile
from pcbasm.pcb.generate import save_board
from pcbasm.pcb.units import KicadError
from tests.helpers import make_paste_flow_calibration_offset_pad_root
from tests.pcbasm.pasting.flowcalib.board.support import (
    CUSTOM_A,
    QFN,
    R0402,
    R1206,
    custom_pad,
)


class TestBoardGenerator:
    """設定とcatalogをまとめて返すorchestration契約."""

    def test_adds_a_named_custom_pad_to_resolved_config(self, generator):
        resolved = generator.add_custom_pad(
            BoardConfig(),
            CustomPadDraft(
                shape="roundrect",
                width_mm=1.2,
                height_mm=0.8,
                corner_radius_mm=0.2,
                name="試験用パッド",
            ),
        )

        assert len(resolved.config.custom_pads) == 1
        assert resolved.catalog[-1].footprint_label == "試験用パッド"
        assert resolved.catalog[-1].label == "角丸矩形 · 1.2 × 0.8 mm · R0.2 mm"

    def test_uses_shape_and_dimensions_as_default_name(self, generator):
        resolved = generator.add_custom_pad(
            BoardConfig(),
            CustomPadDraft(shape="oval", width_mm=1.5, height_mm=0.5),
        )

        assert resolved.config.custom_pads[-1].name == ("長円（スロット） 1.5 × 0.5 mm")
        assert resolved.catalog[-1].footprint_label == ("長円（スロット） 1.5 × 0.5 mm")

    def test_circle_draft_needs_only_its_diameter(self, generator):
        resolved = generator.add_custom_pad(
            BoardConfig(),
            CustomPadDraft(shape="circle", width_mm=0.75),
        )

        custom_pad = resolved.config.custom_pads[-1]
        assert custom_pad.height_mm == 0.75
        assert custom_pad.corner_radius_mm == 0.0
        assert custom_pad.name == "円 φ0.75 mm"

    def test_custom_pad_addition_recovers_an_empty_pattern_config(self, generator):
        resolved = generator.add_custom_pad(
            BoardConfig(patterns=()),
            CustomPadDraft(shape="circle", width_mm=0.75),
        )

        assert len(resolved.config.patterns) == 1
        assert resolved.config.patterns[0].catalog_id.startswith("custom:")

    def test_footprint_addition_recovers_an_empty_pattern_config(self, generator):
        addition = generator.add_footprint_patterns(BoardConfig(patterns=()), QFN)

        assert addition.added_count == 3
        assert len(addition.config.patterns) == 3

    def test_adds_all_distinct_footprint_patterns_with_catalog_defaults(
        self, generator
    ):
        addition = generator.add_footprint_patterns(BoardConfig(), QFN)
        added_patterns = addition.config.patterns[-addition.added_count :]
        added_catalog = addition.catalog[-addition.added_count :]

        assert addition.added_count == 3
        assert {item.source_pad_count for item in added_catalog} == {1, 4, 16}
        assert [item.catalog_id for item in added_catalog] == [
            item.catalog_id for item in added_patterns
        ]
        assert [
            (
                pattern.rotation_span_deg,
                pattern.rotation_count,
                pattern.repeat_count,
            )
            for pattern in added_patterns
        ] == [
            (
                item.default_rotation_span_deg,
                item.default_rotation_count,
                item.default_repeat_count,
            )
            for item in added_catalog
        ]

    def test_adding_an_existing_footprint_is_a_no_op(self, generator):
        first = generator.add_footprint_patterns(BoardConfig(), QFN)

        duplicate = generator.add_footprint_patterns(first.config, QFN)

        assert duplicate.added_count == 0
        assert duplicate.config == first.config
        assert duplicate.catalog == first.catalog

    def test_preview_returns_resolved_config_catalog_and_layout(self, generator):
        preview = generator.preview(BoardConfig())

        assert preview.config.patterns
        assert [item.catalog_id for item in preview.catalog] == [
            pattern.catalog_id for pattern in preview.config.patterns
        ]
        assert preview.layout.pad_count == 64
        assert preview.overflow_message is None
        assert preview.layout.placement_area.width == 38.0
        assert preview.layout.preview_bounds.width == 40.0
        assert preview.layout.pads[0].display_name.startswith("R_0402_1005Metric / ")

    def test_preview_keeps_all_geometry_when_layout_overflows(self, generator):
        config = BoardConfig(
            board=BoardSpec(width_mm=10.0, height_mm=10.0),
            patterns=(PatternSpec(R1206),),
        )

        preview = generator.preview(config)

        assert preview.overflow_message is not None
        assert "自動最適配置" in preview.overflow_message
        assert preview.layout.pad_count == 12
        area = preview.layout.placement_area
        assert any(
            pad.bounds.x + pad.bounds.width > area.x + area.width + 1e-9
            or pad.bounds.y + pad.bounds.height > area.y + area.height + 1e-9
            for pad in preview.layout.pads
        )
        assert (
            preview.layout.preview_bounds.width > config.board.width_mm
            or preview.layout.preview_bounds.height > config.board.height_mm
        )

        assert generator.layout(config) == (None, preview.overflow_message)

    def test_preview_marks_purge_pad_outside_the_placement_area(self, generator):
        config = BoardConfig(
            board=BoardSpec(width_mm=10.0, height_mm=10.0),
            purge_pad=PurgePadSpec(
                width_mm=9.0,
                height_mm=2.0,
            ),
            patterns=(PatternSpec(R0402, repeat_count=1),),
        )

        preview = generator.preview(config)

        assert preview.overflow_message is not None
        assert "purge pad幅" in preview.overflow_message
        area = preview.layout.placement_area
        purge = preview.layout.purge_pad
        assert purge.x + purge.width > area.x + area.width
        assert preview.layout.pad_count == 4
        assert len(preview.layout.purge_polygons) == 2
        assert preview.layout.preview_bounds.width == config.board.width_mm

        assert generator.board_bytes(config) == (None, preview.overflow_message)

    def test_preview_keeps_every_pad_when_total_layout_overflows(self, generator):
        config = BoardConfig(
            board=BoardSpec(width_mm=8.0, height_mm=8.0),
            custom_pads=(custom_pad(CUSTOM_A, "A", width_mm=3.0, height_mm=3.0),),
            patterns=(PatternSpec(CUSTOM_A, 180.0, 1, 3),),
        )

        preview = generator.preview(config)

        assert preview.overflow_message is not None
        assert "自動最適配置" in preview.overflow_message
        assert preview.layout.pad_count == 3
        assert {pad.catalog_id for pad in preview.layout.pads} == {CUSTOM_A}
        area = preview.layout.placement_area
        assert any(
            pad.bounds.x + pad.bounds.width > area.x + area.width + 1e-9
            or pad.bounds.y + pad.bounds.height > area.y + area.height + 1e-9
            for pad in preview.layout.pads
        )

        assert generator.board_bytes(config) == (None, preview.overflow_message)

    def test_shared_generator_is_safe_for_concurrent_real_pcbnew_calls(self, generator):
        workers = 4
        ready = threading.Barrier(workers, timeout=10.0)
        observations: list[tuple[int, tuple[str, ...]] | None] = [None] * workers
        errors: list[Exception | None] = [None] * workers

        def observe_preview(index: int) -> None:
            try:
                ready.wait()
                preview = generator.preview(BoardConfig())
                observations[index] = (
                    preview.layout.pad_count,
                    tuple(item.catalog_id for item in preview.catalog),
                )
            except Exception as exc:
                errors[index] = exc

        threads = [
            threading.Thread(target=observe_preview, args=(index,), daemon=True)
            for index in range(workers)
        ]
        for thread in threads:
            thread.start()
        deadline = monotonic() + 15.0
        for thread in threads:
            thread.join(max(0.0, deadline - monotonic()))

        assert not [
            thread for thread in threads if thread.is_alive()
        ], "共有Generatorの並行previewが15秒以内に完了しませんでした"
        assert not [error for error in errors if error is not None]
        completed = [item for item in observations if item is not None]

        assert len(completed) == workers
        assert len(set(completed)) == 1
        assert completed[0][0] == 64


class TestBoardCoordinateLimits:
    """実footprint由来の座標範囲エラー契約."""

    @pytest.fixture
    def offset_anchor_overflow(
        self, tmp_path: Path
    ) -> tuple[BoardGenerator, BoardConfig]:
        root = make_paste_flow_calibration_offset_pad_root(
            tmp_path / "footprints", shape_offset_x_mm=-2_146.0
        )
        generator = BoardGenerator(root)
        config = BoardConfig(
            patterns=(
                PatternSpec(
                    "Test.pretty/OffsetPad#pad-0",
                    rotation_count=1,
                    repeat_count=1,
                ),
            )
        )
        return generator, config

    def test_preview_rejects_offset_anchor_overflow(
        self,
        offset_anchor_overflow: tuple[BoardGenerator, BoardConfig],
    ):
        generator, config = offset_anchor_overflow

        with pytest.raises(KicadError, match="座標範囲"):
            generator.preview(config)

    def test_build_board_rejects_offset_anchor_overflow(
        self,
        offset_anchor_overflow: tuple[BoardGenerator, BoardConfig],
    ):
        generator, config = offset_anchor_overflow

        with pytest.raises(KicadError, match="座標範囲"):
            generator.build_board(config)


class TestBoardGeneration:
    """生成した実KiCad基板のround-trip."""

    @pytest.fixture
    def board(self, generator) -> pcbnew.BOARD:
        board, overflow_message = generator.build_board(BoardConfig())
        assert overflow_message is None
        assert board is not None
        return board

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
            "PURGE",
            "PAD1",
            "PAD64",
        }

    def test_qfn_variants_keep_their_real_source_layers(self, generator):
        variants = generator.pad_patterns_for(QFN)
        config = BoardConfig(
            patterns=tuple(
                PatternSpec(item.catalog_id, rotation_count=1, repeat_count=1)
                for item in variants
            )
        )

        board, _overflow_message = generator.build_board(config)
        assert board is not None
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

    @pytest.mark.parametrize(
        ("shape", "width", "height", "radius", "expected_shape"),
        [
            ("circle", 1.0, 1.0, 0.0, pcbnew.PAD_SHAPE_CIRCLE),
            ("rectangle", 1.2, 0.8, 0.0, pcbnew.PAD_SHAPE_RECTANGLE),
            ("roundrect", 1.2, 0.8, 0.2, pcbnew.PAD_SHAPE_ROUNDRECT),
            ("oval", 1.5, 0.5, 0.0, pcbnew.PAD_SHAPE_OVAL),
        ],
    )
    def test_custom_pad_shapes_are_written_as_real_kicad_pads(
        self, generator, shape, width, height, radius, expected_shape
    ):
        config = BoardConfig(
            custom_pads=(custom_pad(CUSTOM_A, shape, shape, width, height, radius),),
            patterns=(
                PatternSpec(
                    CUSTOM_A,
                    rotation_count=1,
                    repeat_count=1,
                ),
            ),
        )

        board, _overflow_message = generator.build_board(config)
        assert board is not None
        footprint = next(
            item for item in board.GetFootprints() if item.GetReference() == "PAD1"
        )
        pad = next(iter(footprint.Pads()))

        assert pad.GetShape() == expected_shape
        assert pad.GetSize().x == pytest.approx(pcbnew.FromMM(width), abs=1)
        assert pad.GetSize().y == pytest.approx(pcbnew.FromMM(height), abs=1)
        assert pad.GetLayerSet().Contains(pcbnew.F_Cu)
        assert pad.GetLayerSet().Contains(pcbnew.F_Paste)

    def test_generated_board_round_trip_keeps_outline_and_pad_layers(
        self, pcb: PcbFile
    ):
        assert pcb.outline.width == pytest.approx(40.0, abs=0.1)
        assert pcb.outline.height == pytest.approx(40.0, abs=0.1)
        assert len(pcb.components) == 65
        assert len(pcb.pads) == 65
        assert all(pad.polygon.area > 0 for pad in pcb.pads)
        assert all(pad.copper_polygon.area > 0 for pad in pcb.pads)
        purge = [pad for pad in pcb.pads if pad.designator == "PURGE"]
        assert len(purge) == 1
        assert purge[0].pad_number == "1"
        assert purge[0].polygon.area == pytest.approx(4.0, abs=0.01)

    def test_rotation_is_written_to_pad_footprints(self, pcb: PcbFile):
        rotations = {
            component.rotation
            for component in pcb.components
            if component.designator.startswith("PAD")
        }

        assert rotations == {0.0, 45.0, 90.0, 135.0}

    def test_exported_documents_use_schema_one(self, generator):
        config_payload = generator.config_bytes(BoardConfig())
        board_payload, _overflow_message = generator.board_bytes(BoardConfig())
        assert board_payload is not None

        assert b'"schema_version": 1' in config_payload
        assert board_payload.startswith(b"(kicad_pcb")
