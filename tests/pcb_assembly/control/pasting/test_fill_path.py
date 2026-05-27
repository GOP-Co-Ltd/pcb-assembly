"""build_paste_fill_path のテスト."""

import pytest
from shapely import Polygon
from shapely.geometry import Point as ShapelyPoint

from pcb_assembly.control.pasting.fill_path import (
    _generate_linear_path,
    _generate_spiral_path,
    build_paste_fill_path,
)
from pcb_assembly.geometry import Point2d


class TestBuildPasteFillPath:
    """build_paste_fill_path 関数のテスト."""

    def test_normal_polygon_picks_spiral(self):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])

        result = build_paste_fill_path(polygon, nozzle_diameter=1.0)

        assert len(result) > 2

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (0.3, 2.0, 0.34),
            (0.8, 5.0, 1.0),
        ],
    )
    def test_narrow_polygon_picks_linear(self, width, length, nozzle_diameter):
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])

        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        assert len(result) == 2
        expected_distance = max(width, length) - nozzle_diameter
        assert (result[1] - result[0]).norm == pytest.approx(
            expected_distance, abs=0.01
        )
        short_axis_value = min(width, length) / 2
        if width < length:
            assert result[0].x == pytest.approx(short_axis_value, abs=0.01)
            assert result[1].x == pytest.approx(short_axis_value, abs=0.01)
        else:
            assert result[0].y == pytest.approx(short_axis_value, abs=0.01)
            assert result[1].y == pytest.approx(short_axis_value, abs=0.01)

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (0.1, 0.1, 1.0),
            (1.0, 0.5, 1.0),
        ],
    )
    def test_too_small_polygon_returns_empty(self, width, length, nozzle_diameter):
        polygon = Polygon([(0, 0), (width, 0), (width, length), (0, length)])

        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        assert result == []

    @pytest.mark.parametrize("nozzle_diameter", [0.0, -1.0, -0.001])
    def test_invalid_nozzle_diameter_raises_value_error(self, nozzle_diameter):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])

        with pytest.raises(ValueError, match="nozzle_diameter"):
            build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

    def test_empty_polygon_returns_empty(self):
        assert build_paste_fill_path(Polygon(), nozzle_diameter=1.0) == []

    def test_all_points_are_point2d(self):
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])

        result = build_paste_fill_path(polygon, nozzle_diameter=1.0)

        assert all(isinstance(p, Point2d) for p in result)


class TestGenerateSpiralPath:
    """_generate_spiral_path 関数のテスト."""

    def test_path_starts_at_center(self):
        """先頭点が polygon.representative_point() と一致すること."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        result = _generate_spiral_path(polygon, line_spacing=1.0, initial_inset=0.5)
        assert len(result) >= 2
        rep = polygon.representative_point()
        assert result[0].x == pytest.approx(rep.x, abs=1e-6)
        assert result[0].y == pytest.approx(rep.y, abs=1e-6)

    def test_simple_rectangle_innermost_first(self):
        """矩形螺旋の先頭点が中心近傍、終端点が外周近傍にあること."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        line_spacing = 1.0
        initial_inset = 0.5

        result = _generate_spiral_path(
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
        result = _generate_spiral_path(
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
        result = _generate_spiral_path(polygon, line_spacing=0.5, initial_inset=0.5)
        assert result == []

    def test_l_shape_covers_both_arms(self):
        """L字形状で両腕に螺旋点が分布すること."""
        polygon = Polygon([(0, 0), (8, 0), (8, 4), (4, 4), (4, 8), (0, 8)])
        result = _generate_spiral_path(polygon, line_spacing=1.0, initial_inset=0.5)
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
        result = _generate_spiral_path(polygon, line_spacing=0.8, initial_inset=0.4)
        assert len(result) > 0
        assert any(p.x < 4 for p in result)  # 左ローブ
        assert any(p.x > 6 for p in result)  # 右ローブ

    @pytest.mark.parametrize("line_spacing", [0.0, -1.0])
    def test_invalid_line_spacing_raises_value_error(self, line_spacing):
        """line_spacing が0以下で ValueError."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        with pytest.raises(ValueError, match="line_spacing"):
            _generate_spiral_path(polygon, line_spacing=line_spacing, initial_inset=0.5)

    @pytest.mark.parametrize("initial_inset", [-0.1, -1.0])
    def test_invalid_initial_inset_raises_value_error(self, initial_inset):
        """initial_inset が負で ValueError."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        with pytest.raises(ValueError, match="initial_inset"):
            _generate_spiral_path(
                polygon, line_spacing=1.0, initial_inset=initial_inset
            )

    def test_all_points_are_point2d(self):
        """戻り値がすべて Point2d であること."""
        polygon = Polygon([(0, 0), (10, 0), (10, 6), (0, 6)])
        result = _generate_spiral_path(polygon, line_spacing=1.0, initial_inset=0.5)
        assert len(result) > 0
        assert all(isinstance(p, Point2d) for p in result)


class TestGenerateLinearPath:
    """_generate_linear_path 関数のテスト."""

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
        result = _generate_linear_path(polygon, end_inset=end_inset)

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
        result = _generate_linear_path(polygon, end_inset=end_inset)
        assert result == []

    def test_invalid_end_inset_raises_value_error(self):
        """end_inset が負で ValueError."""
        polygon = Polygon([(0, 0), (1, 0), (1, 5), (0, 5)])
        with pytest.raises(ValueError, match="end_inset"):
            _generate_linear_path(polygon, end_inset=-0.1)
