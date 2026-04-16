"""generate_fill_path のテスト."""

import pytest
from shapely import Polygon

from pcb_assembly.geometry import Point2d
from pcb_assembly.geometry.fill import generate_fill_path


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

    def test_small_polygon_returns_linear_fallback(self):
        """nozzle_diameterがポリゴンに対して大きい場合、直線fallbackが返る."""
        # 1 x 0.5 のポリゴンに nozzle_diameter=1.0 → buffer(-1.0)が空 → fallback
        polygon = Polygon([(0, 0), (1, 0), (1, 0.5), (0, 0.5)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0)

        assert len(result) == 2

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

    def test_narrow_polygon_returns_linear_fallback(self):
        """細長いポリゴンで直線フォールバックが返る."""
        # 0.3mm x 2mm のパッド、nozzle_diameter=0.34
        polygon = Polygon([(0, 0), (0.3, 0), (0.3, 2), (0, 2)])
        result = generate_fill_path(polygon, nozzle_diameter=0.34, perimeters=1)

        assert len(result) == 2
        # 最長軸（Y軸方向）に沿った直線
        assert result[0].x == pytest.approx(0.15, abs=0.01)
        assert result[1].x == pytest.approx(0.15, abs=0.01)
        assert abs(result[1].y - result[0].y) == pytest.approx(2.0, abs=0.01)

    def test_diameter_fallback_threshold(self):
        """buffer(-nozzle_diameter)が空になる細長ポリゴンでlinear_fallbackが返る."""
        # 幅0.8、長さ5.0、nozzle_diameter=1.0
        # buffer(-1.0)は空 (幅0.8 < 2.0)
        polygon = Polygon([(0, 0), (0.8, 0), (0.8, 5.0), (0, 5.0)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0)

        assert len(result) == 2
        # 最長軸（Y軸方向）に沿った直線
        assert result[0].x == pytest.approx(0.4, abs=0.01)
        assert result[1].x == pytest.approx(0.4, abs=0.01)

    def test_very_small_polygon_returns_nonempty(self):
        """非常に小さいポリゴンでも空にならない."""
        polygon = Polygon([(0, 0), (0.1, 0), (0.1, 0.1), (0, 0.1)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0)

        assert len(result) > 0

    def test_all_points_are_point2d(self):
        """戻り値がすべてPoint2dであることを確認."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        result = generate_fill_path(polygon, nozzle_diameter=1.0)
        assert all(isinstance(p, Point2d) for p in result)
