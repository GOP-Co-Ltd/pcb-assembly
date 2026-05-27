"""build_paste_fill_path のテスト.

公開 API は :func:`build_paste_fill_path` のみ。線形分岐・螺旋分岐は
「ポリゴン形状 × ノズル径」の組み合わせで誘発し、公開 API 経由で
振る舞い（戻り値の契約）を検証する。内部ヘルパーは直接呼ばない。
"""

import pytest
from shapely import Polygon
from shapely.geometry import Point as ShapelyPoint

from pcbasm.geometry import Point2d
from pcbasm.pasting.fill_path import build_paste_fill_path


class TestInvalidInput:
    """入力バリデーションの振る舞い."""

    @pytest.mark.parametrize("nozzle_diameter", [0.0, -1.0, -0.001])
    def test_non_positive_nozzle_diameter_raises_value_error(self, nozzle_diameter):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])

        with pytest.raises(ValueError, match="nozzle_diameter"):
            build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

    def test_empty_polygon_returns_empty(self):
        result = build_paste_fill_path(Polygon(), nozzle_diameter=1.0)

        assert result == []

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (0.1, 0.1, 1.0),  # ノズルより小さい正方形
            (1.0, 0.5, 1.0),  # 線形分岐だが長軸 <= 2*inset
        ],
    )
    def test_polygon_too_small_for_nozzle_returns_empty(
        self, width, length, nozzle_diameter
    ):
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])

        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        assert result == []


class TestLinearBranch:
    """細い／小さいポリゴンで誘発される線形分岐の振る舞い.

    ``polygon.buffer(-nozzle_diameter)`` が空になる細長ポリゴンを与えると
    線形パスが選ばれる。線形パスは最長軸に沿った2点で、両端が
    ``nozzle_diameter / 2`` だけ内側に補正されることを契約とする。
    """

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (0.3, 2.0, 0.34),
            (0.8, 5.0, 1.0),
        ],
    )
    def test_narrow_polygon_yields_two_point_path(self, width, length, nozzle_diameter):
        # Arrange: buffer(-nozzle) が空になる細長矩形（線形分岐を誘発）
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])
        assert polygon.buffer(-nozzle_diameter).is_empty  # 前提: 線形分岐に入る

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 2点パス
        assert len(result) == 2

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (0.3, 2.0, 0.34),
            (0.8, 5.0, 1.0),
        ],
    )
    def test_linear_path_runs_along_longest_axis_with_end_inset(
        self, width, length, nozzle_diameter
    ):
        # Arrange
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: パス長 = 最長軸 - 2 * (nozzle/2) = 最長軸 - nozzle
        long_axis = max(width, length)
        expected_path_length = long_axis - nozzle_diameter
        assert (result[1] - result[0]).norm == pytest.approx(
            expected_path_length, abs=0.01
        )

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (0.3, 2.0, 0.34),
            (0.8, 5.0, 1.0),
        ],
    )
    def test_linear_path_centered_on_short_axis(self, width, length, nozzle_diameter):
        # Arrange: 長軸方向は y（length > width）なので x が短軸中央で一定
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 両端点が短軸中央に揃う
        short_axis_mid = min(width, length) / 2
        constant_axis = "x" if length >= width else "y"
        assert getattr(result[0], constant_axis) == pytest.approx(
            short_axis_mid, abs=0.01
        )
        assert getattr(result[1], constant_axis) == pytest.approx(
            short_axis_mid, abs=0.01
        )


class TestSpiralBranch:
    """十分大きいポリゴンで誘発される螺旋分岐の振る舞い.

    ``polygon.buffer(-nozzle_diameter)`` が空でない大きなポリゴンを与えると
    螺旋パスが選ばれる。先頭点が中心（``representative_point()``）、終端が
    外周近傍、全点がポリゴン内包、という契約を検証する。
    """

    def test_large_polygon_yields_multi_point_path(self):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        assert not polygon.buffer(-1.0).is_empty  # 前提: 螺旋分岐に入る

        result = build_paste_fill_path(polygon, nozzle_diameter=1.0)

        assert len(result) > 2

    def test_spiral_starts_at_representative_point(self):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])

        result = build_paste_fill_path(polygon, nozzle_diameter=1.0)

        rep = polygon.representative_point()
        assert result[0].x == pytest.approx(rep.x, abs=1e-6)
        assert result[0].y == pytest.approx(rep.y, abs=1e-6)

    def test_spiral_all_points_inside_polygon(self):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])

        result = build_paste_fill_path(polygon, nozzle_diameter=1.0)

        # 数値誤差吸収のため微小バッファで内包判定
        buffer = polygon.buffer(1e-9)
        for p in result:
            assert buffer.contains(ShapelyPoint(p.x, p.y))

    def test_spiral_first_point_near_center_last_near_exterior(self):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        nozzle_diameter = 1.0
        # initial_inset = nozzle/2, line_spacing = nozzle
        initial_inset = nozzle_diameter / 2

        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # 先頭はセントロイド近傍（innermost-first）
        centroid = Point2d(polygon.centroid.x, polygon.centroid.y)
        assert (result[0] - centroid).norm <= 3.0

        # 終端は外周近傍（initial_inset + line_spacing/2 程度の距離内）
        last_dist = polygon.exterior.distance(ShapelyPoint(result[-1].x, result[-1].y))
        assert last_dist <= initial_inset + nozzle_diameter / 2 + 1e-6

    @pytest.mark.parametrize(
        ("width", "height", "nozzle_diameter"),
        [
            (10.0, 6.0, 1.0),
            (12.0, 8.0, 0.8),
        ],
    )
    def test_spiral_has_no_abnormal_jumps(self, width, height, nozzle_diameter):
        # 連続セグメント長がポリゴン最長辺を超えないこと（境界外への飛び出しや
        # 復路往復などの異常ジャンプがないことを確認する振る舞い契約）。
        polygon = Polygon([(0, 0), (width, 0), (width, height), (0, height)])

        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)
        assert len(result) >= 2

        max_seg = max((result[i + 1] - result[i]).norm for i in range(len(result) - 1))
        assert max_seg <= max(width, height)

    def test_spiral_covers_both_arms_of_l_shape(self):
        # L字形状: 両腕に螺旋点が分布すること（凹形状追従の振る舞い）。
        polygon = Polygon([(0, 0), (8, 0), (8, 4), (4, 4), (4, 8), (0, 8)])

        result = build_paste_fill_path(polygon, nozzle_diameter=1.0)

        assert len(result) > 0
        assert any(p.x > 4 for p in result)  # 下腕
        assert any(p.y > 4 for p in result)  # 上腕

    def test_spiral_covers_both_lobes_of_dumbbell(self):
        # ダンベル形状: buffer(-nozzle) が途中で分裂しても両ローブに点が出ること。
        polygon = Polygon(
            [
                (0, 0),
                (4, 0),
                (4, 1.4),
                (6, 1.4),
                (6, 0),
                (10, 0),
                (10, 4),
                (6, 4),
                (6, 2.6),
                (4, 2.6),
                (4, 4),
                (0, 4),
            ]
        )

        result = build_paste_fill_path(polygon, nozzle_diameter=0.8)

        assert len(result) > 0
        assert any(p.x < 4 for p in result)  # 左ローブ
        assert any(p.x > 6 for p in result)  # 右ローブ


class TestReturnType:
    """戻り値の型契約."""

    def test_all_points_are_point2d(self):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])

        result = build_paste_fill_path(polygon, nozzle_diameter=1.0)

        assert all(isinstance(p, Point2d) for p in result)
