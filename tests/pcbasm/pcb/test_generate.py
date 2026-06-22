"""`pcbasm.pcb.generate` の仕様テスト.

計画書 webui-phase3.md「pcbasm 昇格」節が契約:

- generate_grid_pcb は scripts/dev/generate_grid_pcb.py からの移動（挙動不変）。
  既存の pcbnew モック方式テストを monkeypatch 先変更で移設
- build_fill_coverage_board / save_board は make_fill_coverage_pcb の
  build_board + 保存処理の一般化（print は持ち込まない）。
  実 pcbnew で tmp_path に保存 → PcbFile で読み戻して検証する
"""

import pytest
from shapely import Polygon

import pcbasm.pcb.generate as generate_module
from pcbasm.pcb import PcbFile
from pcbasm.pcb.generate import (
    build_fill_coverage_board,
    generate_grid_pcb,
    generate_rect_pcb,
    save_board,
)


@pytest.fixture
def mock_pcbnew(monkeypatch, mocker):
    """pcbasm.pcb.generate が参照する pcbnew をモジュールごとモックする."""
    mock_module = mocker.MagicMock()

    # FromMM: 実際と同様にnm変換(整数)を返す
    mock_module.FromMM.side_effect = lambda x: int(x * 1_000_000)

    # VECTOR2I: タプル的に扱えるモック
    mock_module.VECTOR2I.side_effect = lambda x, y: (x, y)

    # 定数
    mock_module.SHAPE_T_SEGMENT = 0
    mock_module.Edge_Cuts = 44
    mock_module.F_Cu = 0
    mock_module.PAD_ATTRIB_SMD = 1
    mock_module.PAD_SHAPE_RECT = 1

    # BOARD
    mock_board = mocker.MagicMock()
    mock_module.BOARD.return_value = mock_board

    # PCB_SHAPE
    mock_module.PCB_SHAPE.side_effect = lambda board: mocker.MagicMock()

    # FOOTPRINT / PAD
    def make_footprint(board):
        fp = mocker.MagicMock()
        fp_pad = mocker.MagicMock()
        mock_module.PAD.side_effect = lambda f: fp_pad
        return fp

    mock_module.FOOTPRINT.side_effect = make_footprint

    monkeypatch.setattr(generate_module, "pcbnew", mock_module)
    return mock_module


class TestGenerateGridPcb:
    """generate_grid_pcb関数のテスト（tests/scripts から移設）."""

    def test_default_2x2_grid(self, mock_pcbnew, tmp_path):
        output = tmp_path / "test.kicad_pcb"
        generate_grid_pcb(size=30, divisions=2, pad_size=1.0, output=output)

        board = mock_pcbnew.BOARD.return_value

        # 4 edge segments + 4 footprints = 8 Add calls
        assert board.Add.call_count == 8

        # SaveBoard called with correct path
        mock_pcbnew.SaveBoard.assert_called_once_with(str(output), board)

    def test_3x3_grid_creates_9_pads(self, mock_pcbnew, tmp_path):
        output = tmp_path / "test.kicad_pcb"
        generate_grid_pcb(size=40, divisions=3, pad_size=0.5, output=output)

        board = mock_pcbnew.BOARD.return_value
        # 4 edges + 9 footprints = 13
        assert board.Add.call_count == 13

    def test_1x1_grid_creates_1_pad(self, mock_pcbnew, tmp_path):
        output = tmp_path / "test.kicad_pcb"
        generate_grid_pcb(size=10, divisions=1, pad_size=0.5, output=output)

        board = mock_pcbnew.BOARD.return_value
        # 4 edges + 1 footprint = 5
        assert board.Add.call_count == 5

    def test_output_directory_created(self, mock_pcbnew, tmp_path):
        output = tmp_path / "sub" / "dir" / "test.kicad_pcb"
        generate_grid_pcb(size=30, divisions=2, pad_size=1.0, output=output)

        assert output.parent.exists()

    def test_footprint_references_are_sequential(self, mock_pcbnew, tmp_path, mocker):
        # FOOTPRINT呼び出しごとにSetReferenceの引数を記録
        references = []

        def track_footprint(board):
            fp = mocker.MagicMock()
            fp.SetReference.side_effect = lambda ref: references.append(ref)
            fp_pad = mocker.MagicMock()
            mock_pcbnew.PAD.side_effect = lambda f: fp_pad
            return fp

        mock_pcbnew.FOOTPRINT.side_effect = track_footprint

        output = tmp_path / "test.kicad_pcb"
        generate_grid_pcb(size=30, divisions=2, pad_size=1.0, output=output)

        assert references == ["P1", "P2", "P3", "P4"]

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
        mock_pcbnew,
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
