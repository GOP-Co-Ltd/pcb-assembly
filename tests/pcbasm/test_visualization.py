"""`pcbasm.visualization` の仕様テスト.

Phase 3 で scripts からレンダラを昇格しパッケージ化した（計画書
webui-phase3.md「pcbasm 昇格」節）:

- polygon_with_holes_patch: 既存テストが import 変更なしで通る（無風確認）
- render_pcb / render_fill_paths: 実 PCB fixture から tmp_path へ PNG 出力 →
  ファイル生成 + cv2 で読めてサイズ > 0（描画内容の厳密検証はしない）

render 系の import はテスト内で行い、昇格完了前でも既存テストの収集を
妨げない。
"""

import cv2
import numpy as np
from matplotlib.path import Path as MplPath
from shapely import Polygon

from pcbasm.visualization import polygon_with_holes_patch
from tests.helpers import TESTING_DATA_DIR

FILL_COVERAGE_PCB = TESTING_DATA_DIR / "fill_coverage" / "fill_coverage.kicad_pcb"


class TestPolygonWithHolesPatch:
    """polygon_with_holes_patch関数のテスト."""

    def test_simple_polygon_without_hole(self):
        """穴なし矩形は閉じた1リングのMOVETO+LINETO×3+CLOSEPOLYになる."""
        polygon = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        patch = polygon_with_holes_patch(
            polygon,
            facecolor="#ff0000",
            edgecolor="#000000",
            alpha=0.5,
            linewidth=1.0,
        )

        codes = np.asarray(patch.get_path().codes).tolist()
        assert codes == [
            MplPath.MOVETO,
            MplPath.LINETO,
            MplPath.LINETO,
            MplPath.LINETO,
            MplPath.LINETO,
            MplPath.CLOSEPOLY,
        ]
        assert patch.get_facecolor() == (1.0, 0.0, 0.0, 0.5)

    def test_polygon_with_hole_reverses_interior_winding(self):
        """穴付きポリゴンはinterior coordsを逆順にしたverts列を返す."""
        exterior = [(0, 0), (10, 0), (10, 10), (0, 10)]
        interior = [(2, 2), (2, 4), (4, 4), (4, 2)]
        polygon = Polygon(exterior, holes=[interior])
        patch = polygon_with_holes_patch(
            polygon,
            facecolor="#00ff00",
            edgecolor="#00ff00",
            alpha=1.0,
            linewidth=0.5,
        )

        verts = np.asarray(patch.get_path().vertices).tolist()
        # exterior ring (shapely閉ループ5点 + 明示closing 1点) + interior ring (5+1点) = 12点
        assert len(verts) == 12
        # exteriorはshapelyの順序を維持
        assert verts[0] == [0.0, 0.0]
        # interior先頭はshapelyの最終点 (逆順化されている)
        assert verts[6] == [2.0, 2.0]
        assert verts[7] == [4.0, 2.0]

    def test_multiple_holes_produce_multiple_subpaths(self):
        """複数の穴はそれぞれ独立したMOVETO+CLOSEPOLYサブパスとして追加される."""
        exterior = [(0, 0), (10, 0), (10, 10), (0, 10)]
        hole_a = [(1, 1), (1, 2), (2, 2), (2, 1)]
        hole_b = [(5, 5), (5, 6), (6, 6), (6, 5)]
        polygon = Polygon(exterior, holes=[hole_a, hole_b])
        patch = polygon_with_holes_patch(
            polygon,
            facecolor="#ffffff",
            edgecolor="#ffffff",
            alpha=1.0,
            linewidth=1.0,
        )
        codes = np.asarray(patch.get_path().codes).tolist()
        # exterior 1個 + hole 2個 = MOVETO×3, CLOSEPOLY×3
        assert codes.count(MplPath.MOVETO) == 3
        assert codes.count(MplPath.CLOSEPOLY) == 3


class TestRenderPcb:
    """render_pcb（extract_pcb スクリプトから昇格）."""

    def test_renders_real_pcb_to_readable_png(self, tmp_path):
        from pcbasm.pcb import PcbFile
        from pcbasm.visualization import render_pcb

        pcb = PcbFile(FILL_COVERAGE_PCB)
        output = tmp_path / "pcb.png"

        render_pcb(pcb.outline, pcb.pads, pcb.copper, pcb.components, output)

        image = cv2.imread(str(output))
        assert image is not None
        assert image.size > 0


class TestRenderFillPaths:
    """render_fill_paths（fill_path_simulate スクリプトから昇格）."""

    def test_renders_fill_paths_to_readable_png(self, tmp_path):
        from pcbasm.pasting.fill_path import build_paste_fill_path
        from pcbasm.pcb import Layer, PadList, PcbFile
        from pcbasm.visualization import render_fill_paths

        pcb = PcbFile(FILL_COVERAGE_PCB)
        pads = PadList(pad for pad in pcb.pads if pad.layer == Layer.TOP)
        assert len(pads) > 0
        paths = [build_paste_fill_path(pad.polygon, 0.4) for pad in pads]
        output = tmp_path / "fill_path.png"

        render_fill_paths(
            outline=pcb.outline,
            pads=pads,
            paths=paths,
            nozzle_diameter=0.4,
            bead_width_factor=1.0,
            overlap=0.0,
            boundary_margin=0.0,
            layer=Layer.TOP,
            output_path=output,
        )

        image = cv2.imread(str(output))
        assert image is not None
        assert image.size > 0
