"""sample_points_in_polygons のテスト."""

import math

import pytest
from shapely import Polygon
from shapely.geometry import Point as ShapelyPoint

from pcb_assembly.geometry import Point2d, sample_points_in_polygons


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
        sum(1 for p in points if polygon.contains(ShapelyPoint(p.x, p.y)))
        for polygon in polygons
    ]


class TestSamplePointsInPolygons:
    """sample_points_in_polygons関数のテスト."""

    def test_large_rectangle_returns_max_samples(self):
        """50mm四方の島から max_samples=9 点がまばらに選ばれる."""
        polygon = _rectangle(0, 0, 50, 50)
        min_radius = 1.5

        result = sample_points_in_polygons(
            [polygon], min_radius=min_radius, min_samples=3, max_samples=9
        )

        assert len(result) == 9

        inner = polygon.buffer(-min_radius)
        for p in result:
            assert inner.contains(ShapelyPoint(p.x, p.y))

        # グリッドステップ(3mm)以上、かつ十分にまばら(10mm以上)
        min_pair = _min_pair_distance(result)
        assert min_pair >= 2.0 * min_radius
        assert min_pair >= 10.0

    def test_small_islands_below_min_samples_raises(self):
        """候補が min_samples に満たない小島群は ValueError."""
        # buffer(-1.5)後にほぼ1候補しか入らない小島2つ
        polygons = [
            _rectangle(0, 0, 3.5, 3.5),
            _rectangle(20, 0, 23.5, 3.5),
        ]

        with pytest.raises(ValueError, match="min_samples"):
            sample_points_in_polygons(
                polygons, min_radius=1.5, min_samples=3, max_samples=9
            )

    def test_min_radius_too_large_raises(self):
        """全島で buffer(-r) が空になると ValueError."""
        polygons = [
            _rectangle(0, 0, 2, 2),
            _rectangle(10, 0, 12, 2),
        ]

        with pytest.raises(ValueError, match="min_samples"):
            sample_points_in_polygons(
                polygons, min_radius=5.0, min_samples=3, max_samples=9
            )

    def test_candidates_between_min_and_max(self):
        """候補数が min〜max 間なら候補全部が返る."""
        # 15x8矩形 + min_radius=1.5: bufferで角が丸くなり中央行(y=4.5)の
        # x=4.5,7.5,10.5 の3候補に絞られる
        polygon = _rectangle(0, 0, 15, 8)

        result = sample_points_in_polygons(
            [polygon], min_radius=1.5, min_samples=3, max_samples=9
        )

        assert len(result) == 3
        for p in result:
            assert 0 <= p.x <= 15
            assert 0 <= p.y <= 8

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
        # 3点は十分に離れている
        min_pair = _min_pair_distance(result)
        assert min_pair >= 20.0

    @pytest.mark.parametrize(
        "max_samples,expected",
        [(1, 1), (2, 2), (5, 5), (50, 50)],
    )
    def test_respects_max_samples_cap(self, max_samples, expected):
        """max_samplesが十分小さい範囲では max_samples 分だけ返る."""
        polygon = _rectangle(0, 0, 50, 50)

        # 50x50で225候補確保できるため、max_samples以上選ばれない
        result = sample_points_in_polygons(
            [polygon], min_radius=1.5, min_samples=1, max_samples=max_samples
        )

        assert len(result) == expected


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

    def test_covers_all_islands_when_islands_equal_target(self):
        """島数 == max_samples なら各島から少なくとも1点は取られる."""
        polygons = _nine_island_polygons()

        result = sample_points_in_polygons(
            polygons, min_radius=1.5, min_samples=3, max_samples=9
        )

        assert len(result) == 9
        counts = _count_points_per_polygon(polygons, result)
        assert all(c >= 1 for c in counts), f"全島から1点以上のはず: {counts}"

    def test_distributes_when_islands_fewer_than_max(self):
        """島数 < max_samples なら全島から最低2点ずつ取られる."""
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
        assert all(c >= 2 for c in counts), f"全島から2点以上のはず: {counts}"

    def test_small_island_contributes_when_in_extreme_position(self):
        """端に位置する小島(容量1)は最大三角形の頂点として採用される."""
        # 島1: buffer後に1候補のみ取れる小島 (端に配置) / 島2,3: 大島
        polygons = [
            _rectangle(0, 0, 7, 7),
            _rectangle(50, 0, 90, 40),
            _rectangle(0, 50, 40, 90),
        ]

        result = sample_points_in_polygons(
            polygons, min_radius=1.5, min_samples=3, max_samples=9
        )

        assert len(result) == 9
        counts = _count_points_per_polygon(polygons, result)
        # 小島は最大三角形のseedとして1点取られる、残りは大島から
        assert counts[0] == 1
        assert counts[1] + counts[2] == 8
        assert counts[1] >= 3 and counts[2] >= 3
