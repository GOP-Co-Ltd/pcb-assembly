"""generate_fill_path のテスト."""

import pytest
from shapely import Polygon
from shapely.geometry import Point as ShapelyPoint

from pcb_assembly.geometry import Point2d
from pcb_assembly.geometry.fill import (
    generate_concentric_rings,
    generate_fill_path,
    generate_linear_path,
    generate_spiral_path,
)


class TestGenerateFillPath:
    """generate_fill_path関数のテスト."""

    def test_rectangle_contour_and_zigzag(self):
        """矩形ポリゴンで外周1周＋ジグザグパスの座標を検証."""
        # nozzle_diameter=1.0 → line_spacing=1.0, inset=0.5
        # 1周目は0.5内側 (0.5,0.5)-(9.5,5.5)、周長18、target=17
        # → (0.5,0.5)→(9.5,0.5)→(9.5,5.5)→(0.5,5.5)→(0.5,1.5)
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0)

        contour = result[:5]
        assert contour == [
            Point2d(0.5, 0.5),
            Point2d(0.5, 5.5),
            Point2d(9.5, 5.5),
            Point2d(9.5, 0.5),
            Point2d(1.5, 0.5),
        ]

        # ジグザグ領域は更に1.0内側 (1.5,1.5)-(8.5,4.5)
        zigzag = result[5:]
        assert len(zigzag) > 0
        for p in zigzag:
            assert 1.5 <= p.x <= 8.5
            assert 1.5 <= p.y <= 4.5

    def test_two_perimeters(self):
        """perimeters=2で外周2周＋ジグザグが生成される."""
        # nozzle_diameter=1.0 → 1周目: 0.5内側、2周目: 1.5内側
        polygon = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0, perimeters=2)

        first_ring = result[:5]
        assert first_ring[0] == Point2d(0.5, 0.5)
        assert first_ring[-1] != first_ring[0]  # 閉じない

        # 2周目: 1.5内側にオフセットされたポリゴン
        second_ring = result[5:10]
        for p in second_ring:
            assert 1.5 <= p.x <= 8.5
            assert 1.5 <= p.y <= 8.5

        # ジグザグ部分がある (2.5内側)
        zigzag = result[10:]
        assert len(zigzag) > 0

    def test_many_perimeters_fills_entirely(self):
        """周回数が十分大きい場合、ジグザグなしで周回のみ."""
        polygon = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0, perimeters=10)

        assert len(result) > 0
        assert all(isinstance(p, Point2d) for p in result)

    def test_perimeters_zero_zigzag_only(self):
        """perimeters=0でジグザグのみ（外周なし）."""
        polygon = Polygon([(0, 0), (4, 0), (4, 3), (0, 3)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0, perimeters=0)

        assert len(result) > 0
        for p in result:
            assert 0.0 <= p.x <= 4.0

    def test_second_ring_starts_near_first_ring_end(self):
        """2周目は1周目の終端に近い点から開始する."""
        polygon = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
        result = generate_fill_path(polygon, nozzle_diameter=2.0, perimeters=2)

        first_ring_end = result[4]  # 1周目の終端
        second_ring_start = result[5]  # 2周目の開始

        # 2周目は1周目終端に最も近い頂点から開始する
        second_ring_points = result[5:9]  # 閉ループの頂点部分
        for p in second_ring_points:
            assert (p - first_ring_end).norm >= (
                second_ring_start - first_ring_end
            ).norm - 1e-6

    def test_concave_l_shape_multiple_segments(self):
        """L字型凹ポリゴンでジグザグ走査線が分割される."""
        # L字: 下部は幅8, 上部は幅4 (大きめにしてジグザグが生成されるように)
        polygon = Polygon([(0, 0), (8, 0), (8, 4), (4, 4), (4, 8), (0, 8)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0)

        boundary_len = 7  # 6頂点 + 閉じる1点
        zigzag = result[boundary_len:]

        # ジグザグが存在すること
        assert len(zigzag) > 0

        # 下部の走査線は右側まで届き、上部は左半分のみ
        y_values = sorted({p.y for p in zigzag})
        low_y = [v for v in y_values if v < 4.0]
        high_y = [v for v in y_values if v > 4.0]

        if low_y:
            low_points = [p for p in zigzag if p.y == pytest.approx(low_y[0])]
            max_x_low = max(p.x for p in low_points)
            assert max_x_low > 4.0  # 下部は幅8の領域

        if high_y:
            high_points = [p for p in zigzag if p.y == pytest.approx(high_y[0])]
            max_x_high = max(p.x for p in high_points)
            assert max_x_high < 4.0  # 上部は幅4の領域

    @pytest.mark.parametrize("diameter", [0.0, -1.0, -0.001])
    def test_invalid_nozzle_diameter_raises_value_error(self, diameter):
        """nozzle_diameterが0以下の場合にValueErrorが発生."""
        polygon = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        with pytest.raises(ValueError, match="nozzle_diameter"):
            generate_fill_path(polygon, nozzle_diameter=diameter)

    def test_negative_perimeters_raises_value_error(self):
        """perimetersが負の場合にValueErrorが発生."""
        polygon = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        with pytest.raises(ValueError, match="perimeters"):
            generate_fill_path(polygon, nozzle_diameter=1.0, perimeters=-1)

    def test_empty_polygon_returns_empty_list(self):
        """空のポリゴンで空リストを返す."""
        result = generate_fill_path(Polygon(), nozzle_diameter=1.0)
        assert result == []

    def test_angle_90_produces_vertical_zigzag(self):
        """angle=90で走査線が垂直になることを検証."""
        polygon = Polygon([(0, 0), (6, 0), (6, 8), (0, 8)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0, angle=90.0)

        zigzag = result[5:]
        assert len(zigzag) > 0

        # 垂直走査なので各走査線ペアのx座標が同じ
        for j in range(0, len(zigzag) - 1, 2):
            assert zigzag[j].x == pytest.approx(zigzag[j + 1].x, abs=1e-6)

    def test_zigzag_starts_near_contour_end(self):
        """ジグザグが外周終端に近い側から開始される."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0)

        contour_end = result[4]
        zigzag = result[5:]
        assert len(zigzag) > 0

        zigzag_start = zigzag[0]
        zigzag_end = zigzag[-1]
        dist_to_start = (zigzag_start - contour_end).norm
        dist_to_end = (zigzag_end - contour_end).norm
        assert dist_to_start <= dist_to_end

    @pytest.mark.parametrize(
        "width,length,diameter",
        [
            (0.3, 2.0, 0.34),  # 細長パッド
            (0.8, 5.0, 1.0),  # 幅がノズル径の2倍未満
        ],
    )
    def test_narrow_polygon_returns_linear_fallback(self, width, length, diameter):
        """buffer(-nozzle_diameter)が空になる細長ポリゴンで最長軸の直線fallbackが返る."""
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])
        result = generate_fill_path(polygon, nozzle_diameter=diameter)

        assert len(result) == 2
        # 最長軸方向に沿った直線（短辺の中点を結ぶ）、両端をdia/2ずつ内側に補正
        long_axis_length = max(width, length)
        short_axis_mid = min(width, length) / 2
        expected_path_length = long_axis_length - diameter
        assert (result[1] - result[0]).norm == pytest.approx(
            expected_path_length, abs=0.01
        )
        constant_axis = "x" if length >= width else "y"
        assert getattr(result[0], constant_axis) == pytest.approx(
            short_axis_mid, abs=0.01
        )
        assert getattr(result[1], constant_axis) == pytest.approx(
            short_axis_mid, abs=0.01
        )

    @pytest.mark.parametrize(
        "width,length,diameter",
        [
            (0.1, 0.1, 1.0),  # 極小ポリゴン（長辺 < diameter）
            (1.0, 0.5, 1.0),  # 長辺 == diameter
        ],
    )
    def test_too_small_polygon_returns_empty(self, width, length, diameter):
        """長辺がノズル径以下のポリゴンは空リストを返す."""
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])
        result = generate_fill_path(polygon, nozzle_diameter=diameter)

        assert result == []

    def test_all_points_are_point2d(self):
        """戻り値がすべてPoint2dであることを確認."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0)
        assert all(isinstance(p, Point2d) for p in result)


class TestGenerateSpiralPath:
    """generate_spiral_path 関数のテスト."""

    def test_simple_rectangle_innermost_first(self):
        """矩形螺旋の先頭点が中心近傍、終端点が外周近傍にあること."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        line_spacing = 1.0
        initial_inset = 0.5

        result = generate_spiral_path(
            polygon, line_spacing=line_spacing, initial_inset=initial_inset
        )

        assert len(result) > 0

        # 全点がポリゴンに含まれる（数値誤差吸収のため微小バッファ）
        buffer = polygon.buffer(1e-9)
        for p in result:
            assert buffer.contains(ShapelyPoint(p.x, p.y))

        # 先頭はセントロイド近傍（半径3mm程度）
        centroid = Point2d(polygon.centroid.x, polygon.centroid.y)
        assert (result[0] - centroid).norm <= 3.0

        # 終端は外周近傍（initial_inset + line_spacing/2 程度の距離内）
        last_dist = polygon.exterior.distance(ShapelyPoint(result[-1].x, result[-1].y))
        assert last_dist <= initial_inset + line_spacing / 2 + 1e-6

    @pytest.mark.parametrize(
        "width,height,line_spacing,initial_inset",
        [
            (10.0, 6.0, 1.0, 0.5),
            (12.0, 8.0, 0.8, 0.4),
        ],
    )
    def test_outward_step_matches_line_spacing(
        self, width, height, line_spacing, initial_inset
    ):
        """連続セグメント長が概ね line_spacing オーダーで収まること."""
        polygon = Polygon([(0, 0), (width, 0), (width, height), (0, height)])
        result = generate_spiral_path(
            polygon, line_spacing=line_spacing, initial_inset=initial_inset
        )
        assert len(result) >= 2

        # 最大セグメント長はポリゴン最長辺（=最外周の長辺、initial_inset 補正後）
        # を大きく超えないこと。これにより「異常ジャンプ」（境界外への飛び出し
        # や復路の往復など）がないことを確認する。
        max_seg = max((result[i + 1] - result[i]).norm for i in range(len(result) - 1))
        assert max_seg <= max(width, height)

    def test_returns_empty_when_initial_inset_buffer_empty(self):
        """initial_inset で buffer が空になる場合は []."""
        polygon = Polygon([(0, 0), (0.4, 0), (0.4, 0.4), (0, 0.4)])
        result = generate_spiral_path(polygon, line_spacing=0.5, initial_inset=0.5)
        assert result == []

    def test_l_shape_covers_both_arms(self):
        """L字形状で両腕に螺旋点が分布すること."""
        polygon = Polygon([(0, 0), (8, 0), (8, 4), (4, 4), (4, 8), (0, 8)])
        result = generate_spiral_path(polygon, line_spacing=1.0, initial_inset=0.5)
        assert len(result) > 0
        assert any(p.x > 4 for p in result)  # 下腕
        assert any(p.y > 4 for p in result)  # 上腕

    def test_dumbbell_split_handled(self):
        """ダンベル形状（途中で分裂）で両ローブに点が存在すること."""
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
        result = generate_spiral_path(polygon, line_spacing=0.8, initial_inset=0.4)
        assert len(result) > 0
        assert any(p.x < 4 for p in result)  # 左ローブ
        assert any(p.x > 6 for p in result)  # 右ローブ

    @pytest.mark.parametrize("line_spacing", [0.0, -1.0])
    def test_invalid_line_spacing_raises_value_error(self, line_spacing):
        """line_spacing が0以下で ValueError."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        with pytest.raises(ValueError, match="line_spacing"):
            generate_spiral_path(polygon, line_spacing=line_spacing, initial_inset=0.5)

    @pytest.mark.parametrize("initial_inset", [-0.1, -1.0])
    def test_invalid_initial_inset_raises_value_error(self, initial_inset):
        """initial_inset が負で ValueError."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        with pytest.raises(ValueError, match="initial_inset"):
            generate_spiral_path(polygon, line_spacing=1.0, initial_inset=initial_inset)

    def test_all_points_are_point2d(self):
        """戻り値がすべて Point2d であること."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        result = generate_spiral_path(polygon, line_spacing=1.0, initial_inset=0.5)
        assert len(result) > 0
        assert all(isinstance(p, Point2d) for p in result)


class TestGenerateLinearPath:
    """generate_linear_path 関数のテスト."""

    @pytest.mark.parametrize(
        "width,length,end_inset",
        [
            (0.3, 2.0, 0.17),
            (0.8, 5.0, 0.5),
        ],
    )
    def test_long_horizontal_rectangle(self, width, length, end_inset):
        """細長矩形の最長軸に沿った2点パスが生成されること."""
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])
        result = generate_linear_path(polygon, end_inset=end_inset)

        assert len(result) == 2

        long_axis_length = max(width, length)
        short_axis_mid = min(width, length) / 2
        expected_path_length = long_axis_length - 2 * end_inset
        assert (result[1] - result[0]).norm == pytest.approx(
            expected_path_length, abs=0.01
        )

        constant_axis = "x" if length >= width else "y"
        assert getattr(result[0], constant_axis) == pytest.approx(
            short_axis_mid, abs=0.01
        )
        assert getattr(result[1], constant_axis) == pytest.approx(
            short_axis_mid, abs=0.01
        )

    @pytest.mark.parametrize(
        "width,length,end_inset",
        [
            (0.1, 0.1, 0.5),
            (1.0, 0.5, 0.5),
            (1.0, 1.0, 0.5),  # 長軸 == 2*end_inset
        ],
    )
    def test_returns_empty_when_too_short(self, width, length, end_inset):
        """長軸が 2*end_inset 以下のポリゴンは []."""
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])
        result = generate_linear_path(polygon, end_inset=end_inset)
        assert result == []

    def test_invalid_end_inset_raises_value_error(self):
        """end_inset が負で ValueError."""
        polygon = Polygon([(0, 0), (1, 0), (1, 5), (0, 5)])
        with pytest.raises(ValueError, match="end_inset"):
            generate_linear_path(polygon, end_inset=-0.1)


class TestGenerateConcentricRings:
    """generate_concentric_rings 関数のテスト."""

    def test_rectangle_yields_expected_ring_count(self):
        """10x6矩形・spacing=1.0・inset=0.5 で3リング生成されること."""
        # depth: 0.5, 1.5, 2.5 → buffer は (9x5), (7x3), (5x1)
        # depth 3.5 → buffer は (3x-1) → 空
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        rings = generate_concentric_rings(polygon, line_spacing=1.0, initial_inset=0.5)
        assert len(rings) == 3

    def test_innermost_first_ordering(self):
        """innermost first: rings[0] の bbox が rings[-1] より小さいこと."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        rings = generate_concentric_rings(polygon, line_spacing=1.0, initial_inset=0.5)
        assert len(rings) >= 2

        def bbox_area(ring):
            xs = [p.x for p in ring]
            ys = [p.y for p in ring]
            return (max(xs) - min(xs)) * (max(ys) - min(ys))

        assert bbox_area(rings[0]) < bbox_area(rings[-1])

    def test_multipolygon_each_component_gets_rings(self):
        """ダンベル形状で連結成分ごとにリングが得られること."""
        # くびれ幅を 0.4 にして initial_inset=0.4 で確実に分裂させる
        polygon = Polygon(
            [
                (0, 0),
                (4, 0),
                (4, 1.8),
                (6, 1.8),
                (6, 0),
                (10, 0),
                (10, 4),
                (6, 4),
                (6, 2.2),
                (4, 2.2),
                (4, 4),
                (0, 4),
            ]
        )
        rings = generate_concentric_rings(polygon, line_spacing=0.8, initial_inset=0.4)
        assert len(rings) >= 2

        # 分裂深度のリングは別連結成分 → bbox xmin が左右で差がある
        bbox_xmins = sorted({min(p.x for p in r) for r in rings})
        # 左ローブ（xmin 近傍 0.x）と右ローブ（xmin 6台）の双方が存在する
        assert any(x < 2 for x in bbox_xmins)
        assert any(x > 5 for x in bbox_xmins)

    @pytest.mark.parametrize("line_spacing", [0.0, -1.0])
    def test_invalid_line_spacing_raises_value_error(self, line_spacing):
        """line_spacing が0以下で ValueError."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        with pytest.raises(ValueError, match="line_spacing"):
            generate_concentric_rings(
                polygon, line_spacing=line_spacing, initial_inset=0.5
            )

    @pytest.mark.parametrize("initial_inset", [-0.1, -1.0])
    def test_invalid_initial_inset_raises_value_error(self, initial_inset):
        """initial_inset が負で ValueError."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        with pytest.raises(ValueError, match="initial_inset"):
            generate_concentric_rings(
                polygon, line_spacing=1.0, initial_inset=initial_inset
            )
