"""`pcbasm.pcb.generate` の仕様テスト.

計画書 webui-phase3.md「pcbasm 昇格」節が契約:

- generate_grid_pcb は scripts/dev/generate_grid_pcb.py からの移動（挙動不変）
- build_fill_coverage_board / save_board は make_fill_coverage_pcb の
  build_board + 保存処理の一般化（print は持ち込まない）

いずれも実 pcbnew で tmp_path に保存 → PcbFile で読み戻して検証する。
"""

import pytest
from shapely import Polygon

from pcbasm.pcb import PcbFile
from pcbasm.pcb.generate import (
    build_fill_coverage_board,
    generate_grid_pcb,
    generate_rect_pcb,
    save_board,
)


class TestGenerateGridPcb:
    """generate_grid_pcb（実 pcbnew で保存 → PcbFile で読み戻して検証）."""

    @pytest.mark.parametrize("divisions", [1, 2, 3])
    def test_grid_places_one_pad_per_cell(self, tmp_path, divisions: int):
        output = tmp_path / "grid.kicad_pcb"

        generate_grid_pcb(size=30.0, divisions=divisions, pad_size=1.0, output=output)

        pcb = PcbFile(output)
        assert len(pcb.pads) == divisions**2

    @pytest.mark.parametrize(
        ("size", "divisions", "pad_size"),
        [
            (30.0, 0, 1.0),
            (30.0, 2, 0.0),
            (0.0, 2, 1.0),
        ],
    )
    def test_invalid_params_raise_value_error(
        self,
        tmp_path,
        size: float,
        divisions: int,
        pad_size: float,
    ):
        output = tmp_path / "test.kicad_pcb"

        with pytest.raises(ValueError):
            generate_grid_pcb(
                size=size, divisions=divisions, pad_size=pad_size, output=output
            )


class TestGenerateRectPcb:
    """generate_rect_pcb / save_board（実 pcbnew で読み戻し検証）."""

    @pytest.fixture
    def saved_pcb(self, tmp_path) -> PcbFile:
        board = generate_rect_pcb(width=50.0, height=20.0)
        output = tmp_path / "rect.kicad_pcb"
        save_board(board, output)
        return PcbFile(output)

    def test_outline_is_requested_rectangle(self, saved_pcb: PcbFile):
        assert saved_pcb.outline.width == pytest.approx(50.0, abs=0.1)
        assert saved_pcb.outline.height == pytest.approx(20.0, abs=0.1)

    def test_has_no_pads_or_copper(self, saved_pcb: PcbFile):
        assert len(saved_pcb.pads) == 0
        assert len(saved_pcb.copper) == 0

    @pytest.mark.parametrize("width,height", [(0.0, 10.0), (10.0, 0.0), (-1.0, 2.0)])
    def test_invalid_dimensions_raise_value_error(self, width: float, height: float):
        with pytest.raises(ValueError):
            generate_rect_pcb(width=width, height=height)


class TestFillCoverageBoard:
    """build_fill_coverage_board / save_board（実 pcbnew で読み戻し検証）."""

    @pytest.fixture
    def saved_pcb(self, tmp_path) -> PcbFile:
        board = build_fill_coverage_board()
        output = tmp_path / "fill_coverage.kicad_pcb"
        save_board(board, output)
        return PcbFile(output)

    @staticmethod
    def _largest_by_designator(pcb: PcbFile) -> dict[str, Polygon]:
        """Designator → 最大面積の paste polygon（custom pad は L 字本体と アンカー極小円が非連結で複数
        polygon に分かれるため）."""
        largest: dict[str, Polygon] = {}
        for pad in pcb.pads:
            held = largest.get(pad.designator)
            if held is None or pad.polygon.area > held.area:
                largest[pad.designator] = pad.polygon
        return largest

    def test_round_trips_with_same_paste_pads_as_fixture(self, saved_pcb: PcbFile):
        # data/testing/fill_coverage/fill_coverage.kicad_pcb と同一構成
        # （挙動不変の昇格）: 面2 + 凹形3 + L字のアンカー円1 + 線1 + 点1 = 8
        assert len(saved_pcb.pads) == 8
        assert {pad.designator for pad in saved_pcb.pads} == {
            "AREA_RECT",
            "AREA_RR",
            "CONCAVE_L",
            "CONCAVE_DB",
            "SPLIT_DB",
            "LINE_THIN",
            "DOT_TINY",
        }

    def test_outline_is_60x40_mm(self, saved_pcb: PcbFile):
        assert saved_pcb.outline.width == pytest.approx(60.0, abs=0.1)
        assert saved_pcb.outline.height == pytest.approx(40.0, abs=0.1)

    def test_concave_pads_are_nonconvex(self, saved_pcb: PcbFile):
        polygons = self._largest_by_designator(saved_pcb)

        for ref in ("CONCAVE_L", "CONCAVE_DB", "SPLIT_DB"):
            polygon = polygons[ref]
            assert polygon.area < polygon.convex_hull.area * 0.99, ref

    def test_line_pad_is_thin(self, saved_pcb: PcbFile):
        polygons = self._largest_by_designator(saved_pcb)

        min_x, min_y, max_x, max_y = polygons["LINE_THIN"].bounds
        assert min(max_x - min_x, max_y - min_y) < 0.5

    def test_dot_pad_is_tiny(self, saved_pcb: PcbFile):
        polygons = self._largest_by_designator(saved_pcb)

        assert polygons["DOT_TINY"].area < 0.1
