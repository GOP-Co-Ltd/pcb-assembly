"""build_paste_fill_path のテスト.

公開 API は :func:`build_paste_fill_path` のみ。面塗布（外周＋牛耕式ジグザグ）・
線塗布・点塗布のフォールバック階層は「ポリゴン形状 × ノズル径」の組み合わせで
誘発し、公開 API 経由で振る舞い（戻り値の契約）を検証する。内部ヘルパーは
直接呼ばない。

戻り値は成分別ポリラインのリスト ``list[list[Point2d]]``。各内側 ``list[Point2d]``
が 1 連結成分のポリラインを表す。

しきい値はハードコードせず、形状・ノズル径・overlap・margin を parametrize して
そこから導出する（契約メモ §5）。
"""

import math
from typing import Any, cast

import pytest
from shapely import LineString, Polygon
from shapely.affinity import rotate
from shapely.geometry import Point as ShapelyPoint

from pcbasm.geometry import Point2d
from pcbasm.pasting.fill_path import (
    build_pad_fill_plan_for,
    build_paste_fill_path as _build_paste_fill_path,
    build_paste_fill_plan,
)
from pcbasm.pasting.params import PasteParams

# セグメント内包・外周マージン判定の浮動小数誤差を吸収する微小バッファ（定数）。
# ジグザグ端点が外周に乗るため、境界一致を covers が拾えるよう微小に膨らませる。
_EPS = 1e-6
_AUTO_LINE_ASPECT_RATIO = 1.618
_AUTO_AREA_SHORT_SIDE_FACTOR = 3.0
# aspect(line/dot)分岐だけを検証したいとき、面塗布分岐を無効化する大きい倍率。
_AREA_DISABLED_FACTOR = 100.0


def build_paste_fill_path(
    polygon: Polygon,
    nozzle_diameter: float,
    **kwargs: Any,
) -> list[list[Point2d]]:
    """既存の面塗布契約テスト用に新 API の必須引数を明示する。"""
    kwargs.setdefault("dispense_mode", "area")
    kwargs.setdefault("auto_line_aspect_ratio", _AUTO_LINE_ASPECT_RATIO)
    kwargs.setdefault("auto_area_short_side_factor", _AUTO_AREA_SHORT_SIDE_FACTOR)
    return _build_paste_fill_path(polygon, nozzle_diameter, **kwargs)


def _rectangle(width: float, length: float) -> Polygon:
    """原点を一隅とする軸並行の矩形を作る."""
    return Polygon([(0, 0), (width, 0), (width, length), (0, length)])


def _concave_dumbbell() -> Polygon:
    """凹形ダンベル（くびれ幅 1.2）.

    成分分割は誘発しない（buffer(-0.4) でも 1 成分）が、凹形のため面塗布の セグメント内包契約を検証する素材になる。
    """
    return Polygon(
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


def _split_dumbbell_h() -> Polygon:
    """細首ダンベル（水平・くびれ幅 0.3）.

    くびれ幅 0.3 のため inset>0.15（nozzle>0.30）で左右 2 ローブに分裂する。
    """
    return Polygon(
        [
            (0, 0),
            (4, 0),
            (4, 1.85),
            (6, 1.85),
            (6, 0),
            (10, 0),
            (10, 4),
            (6, 4),
            (6, 2.15),
            (4, 2.15),
            (4, 4),
            (0, 4),
        ]
    )


def _split_dumbbell_v() -> Polygon:
    """細首ダンベル（垂直・くびれ幅 0.3）.

    水平版を縦に倒した形。inset>0.15 で上下 2 ローブに分裂する。
    """
    return Polygon(
        [
            (0, 0),
            (4, 0),
            (4, 4),
            (2.15, 4),
            (2.15, 6),
            (4, 6),
            (4, 10),
            (0, 10),
            (0, 6),
            (1.85, 6),
            (1.85, 4),
            (0, 4),
        ]
    )


def _segments(polyline: list[Point2d]) -> list[LineString]:
    """ポリラインの隣接 2 点が作る LineString のリストを返す."""
    return [
        LineString(
            [(polyline[i].x, polyline[i].y), (polyline[i + 1].x, polyline[i + 1].y)]
        )
        for i in range(len(polyline) - 1)
    ]


class TestFallbackHierarchy:
    """形状 × ノズル径で 面 / 線 / 点 のフォールバック段を誘発する.

    - 大きい矩形 → 面塗布（外周＋ジグザグ。内側ポリラインが複数点）
    - 細長い矩形 → 線塗布（1 ポリライン・2 点）
    - 極小パッド → 点塗布（1 ポリライン・1 点）

    いずれの段でも外側リストは空にならない（点フォールバックがあるため）。
    """

    def test_large_polygon_triggers_area_fill(self):
        # Arrange: buffer(-inset) が面として残る十分大きな矩形
        polygon = _rectangle(10.0, 6.0)
        nozzle_diameter = 1.0
        assert not polygon.buffer(-nozzle_diameter / 2).is_empty  # 前提: 面が残る

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 面塗布は外周＋ジグザグで多点ポリラインを少なくとも 1 本含む
        assert len(result) >= 1
        assert any(len(polyline) > 2 for polyline in result)

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (0.3, 2.0, 0.34),
            (0.8, 5.0, 1.0),
        ],
    )
    def test_narrow_polygon_triggers_line_fill(self, width, length, nozzle_diameter):
        # Arrange: buffer(-w/2) が空になる細長矩形（面が残らず線塗布へ）
        polygon = _rectangle(width, length)
        assert polygon.buffer(-nozzle_diameter / 2).is_empty  # 前提: 面が残らない

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 線塗布は 1 ポリライン・2 点
        assert len(result) == 1
        assert len(result[0]) == 2

    def test_tiny_polygon_triggers_dot_fill(self):
        # Arrange: 線塗布の最長軸も 2*end_inset 以下になる極小パッド
        nozzle_diameter = 1.0
        polygon = _rectangle(0.2, 0.2)

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 点塗布は 1 ポリライン・1 点。外側リストは空でない
        assert len(result) == 1
        assert len(result[0]) == 1

    def test_dot_fill_point_inside_polygon(self):
        # Arrange
        polygon = _rectangle(0.2, 0.2)

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=1.0)

        # Assert: 点は polygon の内部代表点
        point = result[0][0]
        assert polygon.buffer(_EPS).covers(ShapelyPoint(point.x, point.y))


class TestDispenseModes:
    """塗布方式の明示指定と Auto 解決を公開 API 経由で検証する。"""

    def test_dot_mode_uses_representative_point(self):
        plan = build_paste_fill_plan(
            _rectangle(10.0, 6.0),
            nozzle_diameter=1.0,
            dispense_mode="dot",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
        )

        assert plan.dispense_mode == "dot"
        assert len(plan.paths) == 1
        assert len(plan.paths[0]) == 1

    def test_line_mode_uses_long_axis_centerline(self):
        plan = build_paste_fill_plan(
            _rectangle(1.0, 4.0),
            nozzle_diameter=0.5,
            dispense_mode="line",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
        )

        assert plan.dispense_mode == "line"
        assert len(plan.paths) == 1
        assert len(plan.paths[0]) == 2

    def test_area_mode_falls_back_to_line_then_dot(self):
        line_plan = build_paste_fill_plan(
            _rectangle(0.8, 5.0),
            nozzle_diameter=1.0,
            dispense_mode="area",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
        )
        dot_plan = build_paste_fill_plan(
            _rectangle(0.2, 0.2),
            nozzle_diameter=1.0,
            dispense_mode="area",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
        )

        assert line_plan.dispense_mode == "line"
        assert len(line_plan.paths[0]) == 2
        assert dot_plan.dispense_mode == "dot"
        assert len(dot_plan.paths[0]) == 1

    def test_auto_uses_minimum_rotated_bbox_aspect_ratio(self):
        rotated = rotate(_rectangle(1.0, 3.0), 35.0, origin=(0.0, 0.0))

        plan = build_paste_fill_plan(
            rotated,
            nozzle_diameter=0.34,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AREA_DISABLED_FACTOR,
        )

        assert plan.dispense_mode == "line"

    @pytest.mark.parametrize(
        ("width", "height", "expected_mode"),
        [
            (1.0, 1.6, "dot"),
            (1.0, 1.7, "line"),
            (1.0, 1.0, "dot"),
        ],
    )
    def test_auto_uses_golden_ratio_threshold(self, width, height, expected_mode):
        plan = build_paste_fill_plan(
            _rectangle(width, height),
            nozzle_diameter=0.34,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AREA_DISABLED_FACTOR,
        )

        assert plan.dispense_mode == expected_mode

    def test_auto_threshold_boundary_is_dot(self):
        plan = build_paste_fill_plan(
            _rectangle(1.0, 2.0),
            nozzle_diameter=0.34,
            dispense_mode="auto",
            auto_line_aspect_ratio=2.0,
            auto_area_short_side_factor=_AREA_DISABLED_FACTOR,
        )

        assert plan.dispense_mode == "dot"

    def test_auto_large_square_triggers_area(self):
        # 短辺が閾値 nozzle*factor を十分上回る大正方形（従来は dot だった動機ケース）
        nozzle_diameter = 0.34
        factor = 3.0
        side = nozzle_diameter * factor * 3.0  # 閾値の 3 倍 → 確実に area

        plan = build_paste_fill_plan(
            _rectangle(side, side),
            nozzle_diameter=nozzle_diameter,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=factor,
        )

        assert plan.dispense_mode == "area"

    def test_auto_wide_rectangle_prefers_area_over_line(self):
        # aspect も短辺閾値も両方満たす矩形。area を line より優先する（順序の要）。
        nozzle_diameter = 0.34
        factor = 3.0
        threshold = nozzle_diameter * factor
        short = threshold * 1.5  # 短辺 > 閾値 → area 条件成立
        long = short * 5.0  # aspect 5 > 1.618 → line 条件も成立するが area が勝つ

        plan = build_paste_fill_plan(
            _rectangle(short, long),
            nozzle_diameter=nozzle_diameter,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=factor,
        )

        assert plan.dispense_mode == "area"

    @pytest.mark.parametrize(
        ("short_delta", "expected_mode"),
        [
            (0.1, "area"),  # 閾値の直上 → area
            (-0.1, "line"),  # 閾値の直下 → line
            (0.0, "line"),  # ちょうど閾値 → 厳密 > なので area にしない
        ],
    )
    def test_auto_short_side_boundary(self, short_delta, expected_mode):
        nozzle_diameter = 0.34
        factor = 3.0
        threshold = nozzle_diameter * factor
        short = threshold + short_delta
        long = (
            threshold * 10.0
        )  # aspect を十分大きく保ち line/area の切り分けを短辺に限定

        plan = build_paste_fill_plan(
            _rectangle(short, long),
            nozzle_diameter=nozzle_diameter,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=factor,
        )

        assert plan.dispense_mode == expected_mode

    def test_auto_narrow_elongated_still_line(self):
        # 短辺 < 閾値 かつ aspect > 縦横比 → 従来どおり line
        nozzle_diameter = 0.34
        factor = 3.0
        threshold = nozzle_diameter * factor
        short = threshold * 0.5  # 閾値未満 → area にはならない
        long = short * 8.0  # aspect 8 > 1.618 → line

        plan = build_paste_fill_plan(
            _rectangle(short, long),
            nozzle_diameter=nozzle_diameter,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=factor,
        )

        assert plan.dispense_mode == "line"

    def test_auto_tiny_still_dot(self):
        # 短辺 < 閾値 かつ aspect < 縦横比 → 従来どおり dot
        nozzle_diameter = 0.34
        factor = 3.0
        threshold = nozzle_diameter * factor
        short = threshold * 0.5
        long = short * 1.2  # aspect 1.2 < 1.618 → dot

        plan = build_paste_fill_plan(
            _rectangle(short, long),
            nozzle_diameter=nozzle_diameter,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=factor,
        )

        assert plan.dispense_mode == "dot"

    def test_auto_area_factor_is_the_knob(self):
        # 同一パッド・同一 aspect で factor だけを振ると area/line が切り替わる。
        nozzle_diameter = 0.34
        short = 1.4
        long = short * 6.0  # aspect 6 > 1.618（factor 大時は area でなく line へ）
        polygon = _rectangle(short, long)

        area_plan = build_paste_fill_plan(
            polygon,
            nozzle_diameter=nozzle_diameter,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=3.0,  # 閾値 1.02 < 1.4 → area
        )
        line_plan = build_paste_fill_plan(
            polygon,
            nozzle_diameter=nozzle_diameter,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=5.0,  # 閾値 1.70 > 1.4 → area にならず line
        )

        assert area_plan.dispense_mode == "area"
        assert line_plan.dispense_mode == "line"

    def test_auto_degenerate_sliver_resolves_without_error(self):
        # ほぼ退化した極薄スライバでも auto がゼロ除算せず解決する（防御的契約）。
        sliver = Polygon([(0, 0), (5, 0), (5, 1e-9), (0, 1e-9)])

        plan = build_paste_fill_plan(
            sliver,
            nozzle_diameter=0.34,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
        )

        assert plan.dispense_mode in {"dot", "line", "area"}


class TestAreaFill:
    """面塗布（外周トレース＋牛耕式ジグザグ）の振る舞い."""

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (10.0, 6.0, 1.0),
            (12.0, 8.0, 0.8),
            (8.0, 8.0, 0.5),
        ],
    )
    def test_area_fill_has_outline_polyline(self, width, length, nozzle_diameter):
        # Arrange
        polygon = _rectangle(width, length)

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 外周をトレースする閉路相当のポリライン（点数が辺数を超える）を含む
        assert len(result) >= 1
        assert any(len(polyline) > 4 for polyline in result)

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (10.0, 6.0, 1.0),  # 長軸 = 幅（x 方向）
            (6.0, 12.0, 1.0),  # 長軸 = 高さ（y 方向）
        ],
    )
    def test_zigzag_runs_along_longest_axis(self, width, length, nozzle_diameter):
        # Arrange: 牛耕式スキャンラインは最長軸方向に走るため、
        # その軸方向の変位が長いセグメントが存在するはず
        polygon = _rectangle(width, length)
        long_axis = "x" if width >= length else "y"

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 最長軸方向に nozzle 径より十分長く走るセグメントが存在する
        long_run = 0.0
        for polyline in result:
            for seg in _segments(polyline):
                (x0, y0), (x1, y1) = list(seg.coords)
                along = abs(x1 - x0) if long_axis == "x" else abs(y1 - y0)
                across = abs(y1 - y0) if long_axis == "x" else abs(x1 - x0)
                if along > across:
                    long_run = max(long_run, along)
        assert long_run > 2 * nozzle_diameter

    @pytest.mark.parametrize(
        ("width", "length", "nozzle_diameter"),
        [
            (10.0, 6.0, 1.0),
            (8.0, 8.0, 0.8),
        ],
    )
    def test_area_fill_coverage_is_roughly_uniform(
        self, width, length, nozzle_diameter
    ):
        # Arrange: パスを bead 幅で buffer した被覆面積が polygon 面積の大半を覆う
        polygon = _rectangle(width, length)
        bead_half = nozzle_diameter / 2

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 全成分パスの buffer 合併 ∩ polygon が polygon 面積の過半を覆う
        covered = None
        for polyline in result:
            line = LineString([(p.x, p.y) for p in polyline])
            band = line.buffer(bead_half)
            covered = band if covered is None else covered.union(band)
        assert covered is not None
        coverage_ratio = covered.intersection(polygon).area / polygon.area
        # しきいは bead 幅 = nozzle 径・行間 = nozzle 径から導出した「概ね均一被覆」
        assert coverage_ratio > 0.6


class TestSegmentContainment:
    """セグメント内包契約（契約メモ §3・最重要・テストの軸）.

    各成分ポリラインの隣接セグメント LineString が、build に渡した
    元 polygon に ``covers`` される（微小 EPS buffer 許容）。元ポリゴン外を
    横切るセグメントが出ないことを保証する。複数成分時は外側リスト長 ≥ 2 かつ
    成分跨ぎの横断セグメントが存在しないこと。
    """

    @pytest.mark.parametrize(
        ("polygon", "nozzle_diameter"),
        [
            (_rectangle(10.0, 6.0), 1.0),
            (_concave_dumbbell(), 0.8),
            (_split_dumbbell_h(), 0.34),
            (_split_dumbbell_v(), 0.34),
        ],
        ids=["rect", "concave", "split_h", "split_v"],
    )
    def test_every_segment_is_covered_by_polygon(self, polygon, nozzle_diameter):
        # Arrange
        buffered = polygon.buffer(_EPS)

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 全成分・全隣接セグメントが元ポリゴン内に収まる（外を横切らない）
        for polyline in result:
            for seg in _segments(polyline):
                assert buffered.covers(seg)

    @pytest.mark.parametrize(
        ("polygon", "nozzle_diameter"),
        [
            (_split_dumbbell_h(), 0.34),
            (_split_dumbbell_v(), 0.34),
        ],
        ids=["split_h", "split_v"],
    )
    def test_split_shape_yields_multiple_components(self, polygon, nozzle_diameter):
        # Arrange: buffer(-inset) が複数成分に割れるノズル径を選ぶ
        inset = nozzle_diameter / 2
        offset = polygon.buffer(-inset)
        # 前提: buffer 後が複数連結成分に分裂する（MultiPolygon）
        assert offset.geom_type == "MultiPolygon"

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 成分が割れるなら外側リスト長 ≥ 2（成分ごとに独立ポリライン）
        assert len(result) >= 2

    @pytest.mark.parametrize(
        ("polygon", "nozzle_diameter"),
        [
            (_split_dumbbell_h(), 0.34),
            (_split_dumbbell_v(), 0.34),
        ],
        ids=["split_h", "split_v"],
    )
    def test_no_cross_component_traversal_segment(self, polygon, nozzle_diameter):
        # Arrange: 各成分ポリラインが単独で内包条件を満たす＝成分跨ぎの長い
        # 横断セグメント（パッド外を通る連結線）が存在しないことを確認する。
        buffered = polygon.buffer(_EPS)

        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: どのセグメントも元ポリゴン内（＝成分間を空中横断する連結線が無い）
        for polyline in result:
            for seg in _segments(polyline):
                assert buffered.covers(seg)


class TestCoverage:
    """Overlap を上げると行間隔 line_spacing = w*(1-overlap) が縮む.

    overlap が大きいほど牛耕式の行間隔が縮み、行数が増える（隣接スキャン間が 狭くなる）。しきいは line_spacing
    の式から導出する。
    """

    @pytest.mark.parametrize("overlap", [0.0, 0.25, 0.5])
    def test_higher_overlap_increases_scanline_density(self, overlap):
        # Arrange: 固定形状・固定ノズル径で overlap のみ変える
        polygon = _rectangle(12.0, 10.0)
        nozzle_diameter = 1.0
        long_axis = "x"  # width >= length なので最長軸は x
        scan_axis = "y"  # スキャンラインは最長軸に垂直 = y 方向に並ぶ

        # Act
        result = build_paste_fill_path(
            polygon, nozzle_diameter=nozzle_diameter, overlap=overlap
        )

        # 最長軸方向に走る（牛耕式の）セグメントの scan 軸座標を集める
        scan_values: set[float] = set()
        for polyline in result:
            for seg in _segments(polyline):
                (x0, y0), (x1, y1) = list(seg.coords)
                along = abs(x1 - x0) if long_axis == "x" else abs(y1 - y0)
                across = abs(y1 - y0) if long_axis == "x" else abs(x1 - x0)
                if along > across:  # 最長軸方向に走る行
                    scan = y0 if scan_axis == "y" else x0
                    scan_values.add(round(scan, 6))
        scan_coords = sorted(scan_values)

        # Assert: スキャン行間隔の中央値が line_spacing = w*(1-overlap) 近傍
        assert len(scan_coords) >= 2
        gaps = [
            scan_coords[i + 1] - scan_coords[i] for i in range(len(scan_coords) - 1)
        ]
        gaps = [g for g in gaps if g > _EPS]
        expected_spacing = nozzle_diameter * (1.0 - overlap)
        median_gap = sorted(gaps)[len(gaps) // 2]
        # 行間隔が期待値の概ね近傍（牛耕端の折り返し誤差を許容）
        assert median_gap == pytest.approx(expected_spacing, rel=0.5)

    def test_overlap_monotonically_increases_row_count(self):
        # Arrange: 同一形状で overlap を上げると牛耕の行数が単調増加する
        polygon = _rectangle(12.0, 10.0)
        nozzle_diameter = 1.0

        def row_count(overlap: float) -> int:
            result = build_paste_fill_path(
                polygon, nozzle_diameter=nozzle_diameter, overlap=overlap
            )
            ys = set()
            for polyline in result:
                for seg in _segments(polyline):
                    (x0, y0), (x1, y1) = list(seg.coords)
                    if abs(x1 - x0) > abs(y1 - y0):  # x 方向（最長軸）に走る行
                        ys.add(round((y0 + y1) / 2, 3))
            return len(ys)

        # Act
        count_low = row_count(0.0)
        count_high = row_count(0.5)

        # Assert: overlap 増 → 行間隔縮 → 行数増
        assert count_high > count_low


class TestExteriorMargin:
    """boundary_margin>0 で全頂点・全セグメントが外周から margin 以上内側."""

    @pytest.mark.parametrize("boundary_margin", [0.1, 0.3])
    def test_all_points_inside_by_margin(self, boundary_margin):
        # Arrange: margin 分内側に縮んだ領域に全頂点が収まること
        polygon = _rectangle(12.0, 10.0)
        nozzle_diameter = 1.0
        # 面塗布領域は polygon.buffer(-(margin + w/2)) なので margin だけでも内側
        shrunk = polygon.buffer(-(boundary_margin - _EPS))

        # Act
        result = build_paste_fill_path(
            polygon,
            nozzle_diameter=nozzle_diameter,
            boundary_margin=boundary_margin,
        )

        # Assert: 全頂点が margin 縮小領域に内包される
        for polyline in result:
            for p in polyline:
                assert shrunk.covers(ShapelyPoint(p.x, p.y))

    @pytest.mark.parametrize("boundary_margin", [0.1, 0.3])
    def test_all_vertices_keep_margin_distance_from_exterior(self, boundary_margin):
        # Arrange
        polygon = _rectangle(12.0, 10.0)
        nozzle_diameter = 1.0

        # Act
        result = build_paste_fill_path(
            polygon,
            nozzle_diameter=nozzle_diameter,
            boundary_margin=boundary_margin,
        )

        # Assert: 各頂点の外周からの距離が margin 以上
        for polyline in result:
            for p in polyline:
                dist = polygon.exterior.distance(ShapelyPoint(p.x, p.y))
                assert dist >= boundary_margin - _EPS

    def test_zero_margin_reaches_closer_to_exterior(self):
        # Arrange: margin=0 のとき面塗布領域が margin>0 より外周に近づく
        polygon = _rectangle(12.0, 10.0)
        nozzle_diameter = 1.0

        def min_exterior_distance(margin: float) -> float:
            result = build_paste_fill_path(
                polygon, nozzle_diameter=nozzle_diameter, boundary_margin=margin
            )
            return min(
                polygon.exterior.distance(ShapelyPoint(p.x, p.y))
                for polyline in result
                for p in polyline
            )

        # Act
        dist_zero = min_exterior_distance(0.0)
        dist_margin = min_exterior_distance(0.3)

        # Assert: margin>0 は外周からより離れる（領域縮小）
        assert dist_margin > dist_zero


class TestInvalidInput:
    """入力バリデーションの振る舞い（契約メモ §1）."""

    @pytest.mark.parametrize("nozzle_diameter", [0.0, -1.0, -0.001])
    def test_non_positive_nozzle_diameter_raises(self, nozzle_diameter):
        polygon = _rectangle(10.0, 6.0)

        with pytest.raises(ValueError, match="nozzle_diameter"):
            build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

    @pytest.mark.parametrize("overlap", [-0.1, 1.0, 1.5, 2.0])
    def test_overlap_out_of_range_raises(self, overlap):
        polygon = _rectangle(10.0, 6.0)

        with pytest.raises(ValueError, match="overlap"):
            build_paste_fill_path(polygon, nozzle_diameter=1.0, overlap=overlap)

    @pytest.mark.parametrize("boundary_margin", [-0.1, -1.0])
    def test_negative_boundary_margin_raises(self, boundary_margin):
        polygon = _rectangle(10.0, 6.0)

        with pytest.raises(ValueError, match="boundary_margin"):
            build_paste_fill_path(
                polygon, nozzle_diameter=1.0, boundary_margin=boundary_margin
            )

    @pytest.mark.parametrize("bead_width_factor", [0.0, -0.5, -1.0])
    def test_non_positive_bead_width_factor_raises(self, bead_width_factor):
        polygon = _rectangle(10.0, 6.0)

        with pytest.raises(ValueError, match="bead_width_factor"):
            build_paste_fill_path(
                polygon, nozzle_diameter=1.0, bead_width_factor=bead_width_factor
            )

    def test_auto_line_aspect_ratio_must_exceed_one(self):
        polygon = _rectangle(10.0, 6.0)

        with pytest.raises(ValueError, match="auto_line_aspect_ratio"):
            build_paste_fill_plan(
                polygon,
                nozzle_diameter=1.0,
                dispense_mode="auto",
                auto_line_aspect_ratio=1.0,
                auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
            )

    @pytest.mark.parametrize("factor", [0.0, -0.5, -1.0])
    def test_auto_area_short_side_factor_must_be_positive(self, factor):
        polygon = _rectangle(10.0, 6.0)

        with pytest.raises(ValueError, match="auto_area_short_side_factor"):
            build_paste_fill_plan(
                polygon,
                nozzle_diameter=1.0,
                dispense_mode="auto",
                auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
                auto_area_short_side_factor=factor,
            )

    def test_unknown_dispense_mode_raises(self):
        polygon = _rectangle(10.0, 6.0)

        with pytest.raises(ValueError, match="未知の塗布方式"):
            build_paste_fill_plan(
                polygon,
                nozzle_diameter=1.0,
                dispense_mode=cast(Any, "spray"),
                auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
                auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
            )

    def test_empty_polygon_returns_empty_list(self):
        result = build_paste_fill_path(Polygon(), nozzle_diameter=1.0)

        assert result == []

    def test_invalid_polygon_returns_empty_list(self):
        # 自己交差する不正ポリゴン（蝶ネクタイ）
        invalid = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
        assert not invalid.is_valid  # 前提: 不正形状

        result = build_paste_fill_path(invalid, nozzle_diameter=1.0)

        assert result == []


class TestReturnType:
    """戻り値の型契約（公開 API 契約ピン）.

    常に ``list[list[Point2d]]``。正常時は外側 ≥ 1・各内側 ≥ 1 点・全要素が
    ``Point2d``。``@pytest.mark.api_contract`` は ``--strict-markers`` 下で未登録
    のため付与せず、通常テストとして契約を固定する（契約メモ §5・spec 裁量）。
    """

    @pytest.mark.parametrize(
        ("polygon", "nozzle_diameter"),
        [
            (_rectangle(10.0, 6.0), 1.0),  # 面
            (_rectangle(0.3, 2.0), 0.34),  # 線
            (_rectangle(0.2, 0.2), 1.0),  # 点
        ],
        ids=["area", "line", "dot"],
    )
    def test_return_is_list_of_polylines_of_point2d(self, polygon, nozzle_diameter):
        # Act
        result = build_paste_fill_path(polygon, nozzle_diameter=nozzle_diameter)

        # Assert: 外側 list、各内側 1 点以上、全要素 Point2d
        assert isinstance(result, list)
        assert len(result) >= 1
        for polyline in result:
            assert isinstance(polyline, list)
            assert len(polyline) >= 1
            assert all(isinstance(p, Point2d) for p in polyline)

    def test_finite_coordinates(self):
        # Act
        result = build_paste_fill_path(_rectangle(10.0, 6.0), nozzle_diameter=1.0)

        # Assert: 全座標が有限値
        for polyline in result:
            for p in polyline:
                assert math.isfinite(p.x)
                assert math.isfinite(p.y)


class TestBuildPadFillPlanFor:
    """build_pad_fill_plan_for は PasteParams から引数対応を単一ソース化する。

    プレビュー（webui router）と実行（PasteApplicator._fill）が同一の対応で
    build_paste_fill_plan を呼ぶための束ね関数。同じ入力に対して build_paste_fill_plan
    の直接呼び出しと同一の計画を返すことを契約とする。
    """

    @staticmethod
    def _paste(**overrides: object) -> PasteParams:
        values: dict = {
            "dispense_mode": "area",
            "line_direction": "unconstrained",
            "paste_height": 0.05,
            "ul_per_mm2": 0.1,
            "prime_extra_delay": 0.0,
            "bead_width_factor": 0.8,
            "overlap": 0.2,
            "boundary_margin": 0.05,
        }
        values.update(overrides)
        return PasteParams(**values)

    @pytest.mark.parametrize("dispense_mode", ["auto", "dot", "line", "area"])
    def test_matches_direct_build_paste_fill_plan(self, dispense_mode: str):
        polygon = _rectangle(2.0, 6.0)
        paste = self._paste(dispense_mode=dispense_mode)

        plan = build_pad_fill_plan_for(
            polygon,
            nozzle_diameter=0.4,
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
            paste=paste,
        )

        expected = build_paste_fill_plan(
            polygon,
            0.4,
            dispense_mode=paste.dispense_mode,
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
            bead_width_factor=paste.bead_width_factor,
            overlap=paste.overlap,
            boundary_margin=paste.boundary_margin,
        )
        assert plan == expected

    def test_invalid_paste_settings_raise_value_error(self):
        with pytest.raises(ValueError):
            build_pad_fill_plan_for(
                _rectangle(2.0, 6.0),
                nozzle_diameter=0.4,
                auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
                auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
                paste=self._paste(overlap=1.5),
            )

    @pytest.mark.parametrize("dispense_mode", ["line", "auto", "area"])
    def test_outward_starts_near_component_for_every_line_resolution(
        self, dispense_mode: str
    ):
        reference = Point2d(0.4, -5.0)

        plan = build_pad_fill_plan_for(
            _rectangle(0.8, 5.0),
            nozzle_diameter=1.0,
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
            paste=self._paste(
                dispense_mode=dispense_mode,
                line_direction="outward",
                boundary_margin=0.0,
            ),
            line_reference=reference,
        )

        assert plan.dispense_mode == "line"
        start, end = plan.paths[0]
        assert (start - reference).norm < (end - reference).norm

    def test_inward_is_the_reverse_of_outward(self):
        polygon = _rectangle(0.8, 5.0)
        reference = Point2d(0.4, -5.0)
        kwargs = {
            "nozzle_diameter": 1.0,
            "auto_line_aspect_ratio": _AUTO_LINE_ASPECT_RATIO,
            "auto_area_short_side_factor": _AUTO_AREA_SHORT_SIDE_FACTOR,
            "line_reference": reference,
        }

        outward = build_pad_fill_plan_for(
            polygon,
            paste=self._paste(
                dispense_mode="line",
                line_direction="outward",
                boundary_margin=0.0,
            ),
            **kwargs,
        )
        inward = build_pad_fill_plan_for(
            polygon,
            paste=self._paste(
                dispense_mode="line",
                line_direction="inward",
                boundary_margin=0.0,
            ),
            **kwargs,
        )

        assert inward.paths[0] == list(reversed(outward.paths[0]))

    def test_directional_line_requires_reference(self):
        with pytest.raises(ValueError) as raised:
            build_pad_fill_plan_for(
                _rectangle(0.8, 5.0),
                nozzle_diameter=1.0,
                auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
                auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
                paste=self._paste(dispense_mode="line", line_direction="outward"),
            )

        assert "部品位置" in str(raised.value)

    def test_unknown_line_direction_raises_value_error(self):
        with pytest.raises(ValueError) as raised:
            build_pad_fill_plan_for(
                _rectangle(0.8, 5.0),
                nozzle_diameter=1.0,
                auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
                auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
                paste=self._paste(line_direction=cast(Any, "sideways")),
                line_reference=Point2d(0.4, -5.0),
            )

        assert "線走行方向" in str(raised.value)

    def test_equal_distance_keeps_unconstrained_order(self):
        polygon = _rectangle(0.8, 5.0)
        reference = Point2d(0.4, 2.5)
        unconstrained = build_pad_fill_plan_for(
            polygon,
            nozzle_diameter=1.0,
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
            paste=self._paste(
                dispense_mode="line",
                line_direction="unconstrained",
                boundary_margin=0.0,
            ),
        )
        outward = build_pad_fill_plan_for(
            polygon,
            nozzle_diameter=1.0,
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
            paste=self._paste(
                dispense_mode="line",
                line_direction="outward",
                boundary_margin=0.0,
            ),
            line_reference=reference,
        )

        assert outward.paths == unconstrained.paths
