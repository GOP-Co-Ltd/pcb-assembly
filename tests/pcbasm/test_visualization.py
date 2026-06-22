"""`pcbasm.visualization` の仕様テスト.

Phase 3 で scripts からレンダラを昇格しパッケージ化した（計画書
webui-phase3.md「pcbasm 昇格」節）:

- polygon_with_holes_patch: 既存テストが import 変更なしで通る（無風確認）
- render_pcb / render_fill_paths: 実 PCB fixture から tmp_path へ PNG 出力 →
  ファイル生成 + cv2 で読めてサイズ > 0（描画内容の厳密検証はしない）

Phase 5 追記（計画書 webui-phase5.md §1「visualization/height_render.py」）:

- render_planned_points / render_height_plane: scripts/pasting/height_plane.py
  の `_visualize_planned` / `_visualize` の昇格。実 PCB fixture（TOP 銅箔を持つ
  led_blinker）+ 合成 HeightPlane から PNG を生成し cv2 で復号可能・非自明サイズ

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

# TOP 銅箔ゾーンを持つ実 PCB（基板背景の銅箔描画を含む height_render 用）
LED_BLINKER_PCB = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"


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
    """render_fill_paths（fill path 可視化処理から昇格）."""

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


def _spread_points_in_outline(pcb, fractions):
    """Outline bbox 内の指定比率位置に Point2d を置く（基板に依存しない配置）."""
    from pcbasm.geometry import Point2d

    minx, miny, maxx, maxy = pcb.outline.polygon.bounds
    return [
        Point2d(minx + fx * (maxx - minx), miny + fy * (maxy - miny))
        for fx, fy in fractions
    ]


class TestHeightRender:
    """render_planned_points / render_height_plane（height_plane scripts
    から昇格）."""

    def test_render_planned_points_outputs_readable_png(self, tmp_path):
        from pcbasm.pcb import PcbFile
        from pcbasm.visualization import render_planned_points

        pcb = PcbFile(LED_BLINKER_PCB)
        # 凸包（薄線描画）が成立する 4 点
        points = _spread_points_in_outline(
            pcb, [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
        )
        output = tmp_path / "planned_points.png"

        render_planned_points(points, pcb, "Planned probe points", output)

        image = cv2.imread(str(output))
        assert image is not None
        assert image.size > 0

    def test_render_planned_points_accepts_fewer_than_hull_points(self, tmp_path):
        """凸包が成立しない 2 点でも描画できる（境界ケース）."""
        from pcbasm.pcb import PcbFile
        from pcbasm.visualization import render_planned_points

        pcb = PcbFile(LED_BLINKER_PCB)
        points = _spread_points_in_outline(pcb, [(0.3, 0.5), (0.7, 0.5)])
        output = tmp_path / "planned_two.png"

        render_planned_points(points, pcb, "Planned (2 points)", output)

        image = cv2.imread(str(output))
        assert image is not None
        assert image.size > 0

    def test_render_height_plane_outputs_readable_png(self, tmp_path):
        from pcbasm.geometry import HeightPlane, Point3d
        from pcbasm.pcb import PcbFile
        from pcbasm.visualization import render_height_plane

        pcb = PcbFile(LED_BLINKER_PCB)
        # 2 次曲面フィットに必要な非退化 6 点（z は緩い傾斜）
        fractions = [
            (0.1, 0.1),
            (0.9, 0.1),
            (0.1, 0.9),
            (0.9, 0.9),
            (0.5, 0.3),
            (0.3, 0.6),
        ]
        points = _spread_points_in_outline(pcb, fractions)
        height_plane = HeightPlane(
            tuple(Point3d(p.x, p.y, 0.01 * p.x + 0.02 * p.y) for p in points)
        )
        output = tmp_path / "height_plane.png"

        render_height_plane(height_plane, pcb, "Height Plane", output)

        image = cv2.imread(str(output))
        assert image is not None
        assert image.size > 0

    def test_render_height_plane_accepts_pcb_to_plane_frame(self, tmp_path):
        from pcbasm.geometry import HeightPlane, Point3d, Shift
        from pcbasm.pcb import PcbFile
        from pcbasm.visualization import render_height_plane

        pcb = PcbFile(LED_BLINKER_PCB)
        pcb_to_plane = Shift(x=120.0, y=-80.0, z=0.0)
        fractions = [
            (0.1, 0.1),
            (0.9, 0.1),
            (0.1, 0.9),
            (0.9, 0.9),
            (0.5, 0.3),
            (0.3, 0.6),
        ]
        board_points = _spread_points_in_outline(pcb, fractions)
        plane_points = [pcb_to_plane.apply(p) for p in board_points]
        height_plane = HeightPlane(
            tuple(Point3d(p.x, p.y, 0.01 * p.x + 0.02 * p.y) for p in plane_points)
        )
        identity_output = tmp_path / "height_plane_identity_frame.png"
        plane_output = tmp_path / "height_plane_machine_frame.png"

        render_height_plane(height_plane, pcb, "Height Plane", identity_output)
        render_height_plane(
            height_plane,
            pcb,
            "Height Plane",
            plane_output,
            pcb_to_plane=pcb_to_plane,
        )

        identity_image = cv2.imread(str(identity_output))
        plane_image = cv2.imread(str(plane_output))
        assert identity_image is not None
        assert plane_image is not None
        assert plane_image.size > 0
        assert plane_image.shape == identity_image.shape
        diff = np.asarray(cv2.absdiff(plane_image, identity_image))
        assert float(diff.mean()) > 0.1
