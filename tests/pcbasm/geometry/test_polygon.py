"""merge_islands / transform_polygon のテスト."""

import pytest
from shapely import Polygon
from shapely.geometry.polygon import LinearRing

from pcbasm.geometry import (
    Compose,
    Identity,
    Point2d,
    Rotation,
    Shift,
    Transform,
    merge_islands,
    transform_polygon,
)


def _rectangle(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


class TestMergeIslands:
    def test_ヘアラインギャップで隣接する矩形はsnapで1つのislandに融合される(self):
        # 1mm 角矩形 2 つが 5µm のギャップで隣接
        left = _rectangle(0.0, 0.0, 1.0, 1.0)
        right = _rectangle(1.005, 0.0, 2.005, 1.0)

        islands = merge_islands([left, right], snap_mm=0.01)

        assert len(islands) == 1
        # closing でギャップが埋まるため、面積は両矩形の合計以上
        assert islands[0].area >= left.area + right.area - 1e-9

    def test_snapを超える実クリアランスで離れた矩形は分離したまま(self):
        # 0.2mm 離れた 2 矩形は snap_mm=0.01 では橋渡しされない
        left = _rectangle(0.0, 0.0, 1.0, 1.0)
        right = _rectangle(1.2, 0.0, 2.2, 1.0)

        islands = merge_islands([left, right], snap_mm=0.01)

        assert len(islands) == 2
        total_area = sum(island.area for island in islands)
        assert total_area == pytest.approx(left.area + right.area)

    def test_重なる矩形はunary_unionで1つのislandに融合される(self):
        lower = _rectangle(0.0, 0.0, 1.0, 1.0)
        upper = _rectangle(0.5, 0.5, 1.5, 1.5)

        islands = merge_islands([lower, upper], snap_mm=0.01)

        assert len(islands) == 1
        # closing は凹コーナーに半径 snap_mm のフィレットを残すため、
        # 面積は snap_mm^2 オーダーでのみ増え得る
        assert islands[0].area == pytest.approx(lower.union(upper).area, abs=1e-3)

    def test_穴付きpolygonの穴は融合後も保存される(self):
        # 10mm 角矩形に 2mm 角の穴（snap_mm より十分大きい穴）
        donut = Polygon(
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
            holes=[[(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)]],
        )

        islands = merge_islands([donut], snap_mm=0.01)

        assert len(islands) == 1
        assert len(islands[0].interiors) == 1
        assert islands[0].area == pytest.approx(donut.area)

    def test_sliver穴はsnapのclosingで除去される(self):
        # 実基板（GENS_Power_Section_5, F.Cu）で確認された症状のピン:
        # pad ポリゴンと zone fill の円弧近似の不一致により、union 後の island に
        # 幅 0.0035〜0.005mm の極細 sliver 穴が残る。closing（snap_mm=0.01）で消えること。
        # ここでは幅 5µm × 長さ 2mm の細長い interior ring を持つ Polygon で再現する。
        sliver_width = 0.005
        with_sliver = Polygon(
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
            holes=[
                [
                    (4.0, 5.0),
                    (6.0, 5.0),
                    (6.0, 5.0 + sliver_width),
                    (4.0, 5.0 + sliver_width),
                ]
            ],
        )

        islands = merge_islands([with_sliver], snap_mm=0.01)

        assert len(islands) == 1
        # sliver 穴（幅 < 2 * snap_mm）は埋められて interior が消える
        assert len(islands[0].interiors) == 0
        # 外形面積はほぼ保存される（sliver 面積 0.01mm^2 が埋まる分のみ増加）
        assert islands[0].area == pytest.approx(100.0, abs=0.02)

    def test_空入力は空リストを返す(self):
        assert merge_islands([], snap_mm=0.01) == []


def _donut() -> Polygon:
    """10mm 角矩形に 2mm 角の穴を持つ穴付きポリゴン."""
    return Polygon(
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
        holes=[[(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)]],
    )


class TestTransformPolygon:
    """transform_polygon のテスト.

    exterior / interiors の各頂点に 2D Transform（transform.apply(Point2d)）を
    適用した Polygon を返す契約。
    """

    @staticmethod
    def _assert_ring_transformed(
        result: LinearRing, original: LinearRing, transform: Transform
    ) -> None:
        """リングの各頂点が transform.apply(Point2d(...)) と一致することを検証する."""
        assert len(result.coords) == len(original.coords)
        for got, orig in zip(result.coords, original.coords, strict=True):
            expected = transform.apply(Point2d(orig[0], orig[1]))
            assert got[0] == pytest.approx(expected.x)
            assert got[1] == pytest.approx(expected.y)

    @pytest.mark.parametrize(
        "transform",
        [
            Shift(2.5, -1.5),
            Rotation(37.0),
            Compose([Rotation(90.0), Shift(1.0, 2.0)]),
        ],
        ids=["shift", "rotation", "compose"],
    )
    def test_穴付き矩形の全頂点にtransformが適用される(self, transform: Transform):
        donut = _donut()

        result = transform_polygon(donut, transform)

        self._assert_ring_transformed(result.exterior, donut.exterior, transform)
        assert len(result.interiors) == len(donut.interiors)
        for got_ring, orig_ring in zip(result.interiors, donut.interiors, strict=True):
            self._assert_ring_transformed(got_ring, orig_ring, transform)

    def test_identityの適用で形状は不変(self):
        donut = _donut()

        result = transform_polygon(donut, Identity())

        assert result.equals(donut)
