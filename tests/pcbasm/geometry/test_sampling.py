"""sample_points_in_polygons / sampling_diagnostics のテスト.

sampling_diagnostics は Phase 5 で scripts/pasting/height_plane.py の
`_print_sampling_diagnostics` / `_clearance_to_copper` の計算部を昇格したもの
（計画書 webui-phase5.md §1）。昇格完了前でも既存テストの収集を妨げないよう、
新 API の import はテスト内で行う。
"""

import math

import pytest
from shapely import Polygon
from shapely.geometry import Point as ShapelyPoint

from pcbasm.geometry import Point2d, sample_points_in_polygons


def _rectangle(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _min_pair_distance(points: list[Point2d]) -> float:
    d = math.inf
    for i, p in enumerate(points):
        for q in points[i + 1 :]:
            d = min(d, math.hypot(p.x - q.x, p.y - q.y))
    return d


def _count_points_per_polygon(
    polygons: list[Polygon], points: list[Point2d]
) -> list[int]:
    """各polygonに含まれる結果点の数を返す."""
    return [
        sum(1 for p in points if polygon.covers(ShapelyPoint(p.x, p.y)))
        for polygon in polygons
    ]


def _min_clearance_to_covering_polygons(
    polygons: list[Polygon], point: Point2d
) -> float:
    shapely_point = ShapelyPoint(point.x, point.y)
    covering = [polygon for polygon in polygons if polygon.covers(shapely_point)]
    return min(polygon.boundary.distance(shapely_point) for polygon in covering)


class TestSamplePointsInPolygons:
    """sample_points_in_polygons関数のテスト."""

    def test_large_rectangle_returns_max_samples(self):
        """50mm四方の島から max_samples=9 点が安全かつ広く選ばれる."""
        polygon = _rectangle(0, 0, 50, 50)
        min_radius = 1.5

        result = sample_points_in_polygons(
            [polygon], min_radius=min_radius, min_samples=3, max_samples=9
        )

        assert len(result) == 9

        # ハードフロア: 全点が銅箔境界から min_radius 以上内側
        inner = polygon.buffer(-min_radius)
        for p in result:
            assert inner.covers(ShapelyPoint(p.x, p.y))

        # 互いに十分まばら (被覆面積が大きい)
        assert _min_pair_distance(result) >= 2.0 * min_radius

    def test_small_islands_below_min_samples_raises(self):
        """候補が min_samples に満たない小島群は ValueError."""
        # 境界寄り候補を追加しても、要求点数に届かない小島群ではエラーにする
        polygons = [
            _rectangle(0, 0, 3.5, 3.5),
            _rectangle(20, 0, 23.5, 3.5),
        ]

        with pytest.raises(ValueError, match="min_samples"):
            sample_points_in_polygons(
                polygons, min_radius=1.5, min_samples=20, max_samples=20
            )

    def test_min_radius_too_large_raises(self):
        """全島で buffer(-r) が空になると候補0で ValueError."""
        polygons = [
            _rectangle(0, 0, 2, 2),
            _rectangle(10, 0, 12, 2),
        ]

        with pytest.raises(ValueError, match="min_samples"):
            sample_points_in_polygons(
                polygons, min_radius=5.0, min_samples=3, max_samples=9
            )

    def test_candidates_below_max_returns_all_distinct(self):
        """候補数が max_samples 未満の小さめ島では候補数ぶん返る (上限以下)."""
        polygon = _rectangle(0, 0, 9, 6)

        result = sample_points_in_polygons(
            [polygon], min_radius=1.5, min_samples=3, max_samples=9
        )

        assert 3 <= len(result) <= 9
        for p in result:
            assert 0 <= p.x <= 9
            assert 0 <= p.y <= 6

    def test_empty_polygons_raises(self):
        """空の polygons は ValueError."""
        with pytest.raises(ValueError, match="min_samples"):
            sample_points_in_polygons([], min_radius=1.5, min_samples=3, max_samples=9)

    def test_max_samples_limits_output(self):
        """大きい島で max_samples=3 に制限される."""
        polygon = _rectangle(0, 0, 50, 50)

        result = sample_points_in_polygons(
            [polygon], min_radius=1.5, min_samples=3, max_samples=3
        )

        assert len(result) == 3
        # 3点は十分に離れている (安全性を保ちつつ広く分散)
        assert _min_pair_distance(result) >= 15.0

    @pytest.mark.parametrize(
        "max_samples,expected",
        [(1, 1), (2, 2), (5, 5), (50, 50)],
    )
    def test_respects_max_samples_cap(self, max_samples, expected):
        """max_samplesが十分小さい範囲では max_samples 分だけ返る."""
        polygon = _rectangle(0, 0, 50, 50)

        result = sample_points_in_polygons(
            [polygon], min_radius=1.5, min_samples=1, max_samples=max_samples
        )

        assert len(result) == expected

    def test_points_respect_min_radius_floor(self):
        """各点は銅箔境界から min_radius 以上の安全余裕を保つ."""
        polygon = _rectangle(0, 0, 50, 50)
        min_radius = 1.5

        result = sample_points_in_polygons(
            [polygon], min_radius=min_radius, min_samples=3, max_samples=9
        )

        for p in result:
            clearance = polygon.boundary.distance(ShapelyPoint(p.x, p.y))
            assert clearance >= min_radius, f"安全余裕不足: {p}, {clearance}"

    def test_explicit_none_outline_keeps_api_compatible(self):
        """Outline=None を明示しても従来の呼び出しと同じように動く."""
        polygon = _rectangle(0, 0, 50, 50)

        result = sample_points_in_polygons(
            [polygon],
            min_radius=1.5,
            min_samples=3,
            max_samples=9,
            outline=None,
        )

        assert len(result) == 9

    def test_small_island_point_lands_at_center(self):
        """小島の点は島の中心付近に来る."""
        # min_samples を満たすため大島2つを添える
        small = _rectangle(0, 0, 7, 7)
        polygons = [
            small,
            _rectangle(50, 0, 90, 40),
            _rectangle(0, 50, 40, 90),
        ]

        result = sample_points_in_polygons(
            polygons, min_radius=1.5, min_samples=3, max_samples=9
        )

        in_small = [p for p in result if small.contains(ShapelyPoint(p.x, p.y))]
        assert in_small, "小島から少なくとも1点は取られるはず"
        for p in in_small:
            assert math.hypot(p.x - 3.5, p.y - 3.5) <= 1.0


def _nine_island_polygons() -> list[Polygon]:
    """中央の大島と周囲8個の小島の合計9島."""
    centers = [
        (10, 10),
        (90, 10),
        (10, 90),
        (90, 90),
        (50, 10),
        (10, 50),
        (90, 50),
        (50, 90),
    ]
    polygons = [_rectangle(30, 30, 70, 70)]
    for cx, cy in centers:
        polygons.append(_rectangle(cx - 3.5, cy - 3.5, cx + 3.5, cy + 3.5))
    return polygons


class TestSpreadAcrossIslands:
    """複数銅箔島にわたる分散性の検証."""

    def test_covers_extreme_corner_islands(self):
        """被覆面積最大化により、四隅の島が確実にサンプルされる."""
        polygons = _nine_island_polygons()
        # polygons[1..4] が四隅の島 (_nine_island_polygons の centers 先頭4つ)

        result = sample_points_in_polygons(
            polygons, min_radius=1.5, min_samples=3, max_samples=9
        )

        assert len(result) == 9
        counts = _count_points_per_polygon(polygons, result)
        assert all(c >= 1 for c in counts[1:5]), f"四隅の島は被覆されるはず: {counts}"

    def test_distributes_across_islands(self):
        """島数 < max_samples なら全島から最低1点は取られる."""
        polygons = [
            _rectangle(0, 0, 30, 30),
            _rectangle(100, 0, 130, 30),
            _rectangle(0, 100, 30, 130),
        ]

        result = sample_points_in_polygons(
            polygons, min_radius=1.5, min_samples=3, max_samples=9
        )

        assert len(result) == 9
        counts = _count_points_per_polygon(polygons, result)
        assert all(c >= 1 for c in counts), f"全島から1点以上のはず: {counts}"

    def test_outline_anchors_cover_outer_rails(self):
        """outline指定時は細い外周銅箔にも四隅・辺寄りの点を取る."""
        outline = _rectangle(0, 0, 100, 100)
        polygons = [
            _rectangle(0, 0, 100, 4),
            _rectangle(0, 96, 100, 100),
            _rectangle(0, 0, 4, 100),
            _rectangle(96, 0, 100, 100),
            _rectangle(35, 35, 65, 65),
        ]

        result = sample_points_in_polygons(
            polygons,
            min_radius=1.0,
            min_samples=6,
            max_samples=9,
            outline=outline,
        )

        assert len(result) == 9
        assert any(p.x < 10 and p.y < 10 for p in result)
        assert any(p.x > 90 and p.y < 10 for p in result)
        assert any(p.x < 10 and p.y > 90 for p in result)
        assert any(p.x > 90 and p.y > 90 for p in result)
        for p in result:
            assert _min_clearance_to_covering_polygons(polygons, p) >= 1.5


class TestSamplingDiagnostics:
    """sampling_diagnostics（probe 計画点の安全余裕と基板カバレッジ）.

    計画書 webui-phase5.md §1 が契約: point_count = 計画点数 / min_clearance =
    点が乗る銅箔境界までの最小距離 [mm] / hull_area_ratio = 計画点凸包の面積 ÷ outline
    面積。points が空なら None。
    """

    def test_known_square_yields_pinned_values(self):
        """10mm 角銅箔上の既知 4 点（凸包は三角形）を数値ピンする.

        - clearance: (2,2)/(8,2)/(5,8) は境界まで 2、(5,5) は 5 → min 2.0
        - 凸包は (2,2)-(8,2)-(5,8) の三角形（面積 18）/ outline 100 → 0.18
        """
        from pcbasm.geometry import sampling_diagnostics

        polygon = _rectangle(0, 0, 10, 10)
        outline = _rectangle(0, 0, 10, 10)
        points = [
            Point2d(2.0, 2.0),
            Point2d(8.0, 2.0),
            Point2d(5.0, 8.0),
            Point2d(5.0, 5.0),
        ]

        diagnostics = sampling_diagnostics(points, [polygon], outline)

        assert diagnostics is not None
        assert diagnostics.point_count == 4
        assert diagnostics.min_clearance == pytest.approx(2.0)
        assert diagnostics.hull_area_ratio == pytest.approx(18.0 / 100.0)

    def test_min_clearance_spans_multiple_islands(self):
        """min_clearance は複数銅箔島の全点にわたる最小値になる."""
        from pcbasm.geometry import sampling_diagnostics

        polygons = [_rectangle(0, 0, 10, 10), _rectangle(20, 0, 30, 10)]
        outline = _rectangle(0, 0, 30, 10)
        # 左島中央（clearance 5）と右島の境界寄り 2 点（clearance 1 / 2）
        points = [Point2d(5.0, 5.0), Point2d(21.0, 5.0), Point2d(25.0, 2.0)]

        diagnostics = sampling_diagnostics(points, polygons, outline)

        assert diagnostics is not None
        assert diagnostics.min_clearance == pytest.approx(1.0)

    def test_empty_points_returns_none(self):
        from pcbasm.geometry import sampling_diagnostics

        polygon = _rectangle(0, 0, 10, 10)

        assert sampling_diagnostics([], [polygon], polygon) is None

    def test_two_points_yield_zero_hull_area_ratio(self):
        """点 2 個の凸包は線分（面積 0）→ hull_area_ratio = 0.0."""
        from pcbasm.geometry import sampling_diagnostics

        polygon = _rectangle(0, 0, 10, 10)
        points = [Point2d(2.0, 5.0), Point2d(8.0, 5.0)]

        diagnostics = sampling_diagnostics(points, [polygon], polygon)

        assert diagnostics is not None
        assert diagnostics.point_count == 2
        assert diagnostics.min_clearance == pytest.approx(2.0)
        assert diagnostics.hull_area_ratio == 0.0

    def test_zero_area_outline_is_defended(self):
        """Outline 面積 0 でもゼロ除算せず hull_area_ratio = 0.0."""
        from pcbasm.geometry import sampling_diagnostics

        polygon = _rectangle(0, 0, 10, 10)
        degenerate_outline = Polygon([(0, 0), (5, 0), (10, 0)])  # 面積 0
        points = [Point2d(2.0, 2.0), Point2d(8.0, 2.0), Point2d(5.0, 8.0)]

        diagnostics = sampling_diagnostics(points, [polygon], degenerate_outline)

        assert diagnostics is not None
        assert diagnostics.hull_area_ratio == 0.0
