"""polygon_with_holes_patch のテスト."""

import numpy as np
from matplotlib.path import Path as MplPath
from shapely import Polygon

from pcb_assembly.visualization import polygon_with_holes_patch


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
