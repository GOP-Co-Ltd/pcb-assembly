"""FillPlan.build / FillPlan.for_pad のテスト.

面塗布（外周＋牛耕式ジグザグ）・線塗布・点塗布のフォールバック階層は
「ポリゴン形状 × ノズル径」の組み合わせで誘発し、公開 API 経由で振る舞い
（戻り値の契約）を検証する。内部ヘルパーは直接呼ばない。

``FillPlan.paths`` は成分別ポリラインの tuple。テスト内の ``build_paste_fill_path``
は既存の面塗布契約テストのために paths を list で返す薄いラッパ。

しきい値はハードコードせず、形状・ノズル径・overlap・margin を parametrize して
そこから導出する（契約メモ §5）。
"""

from typing import Any

import attrs
import pytest
from shapely import LineString, Polygon
from shapely.affinity import rotate
from shapely.geometry import Point as ShapelyPoint

from pcbasm.config import PasteDispenser as PasteDispenserConfig, Toolhead
from pcbasm.geometry import Point2d
from pcbasm.pasting.fill_path import FillPlan
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
    """既存の面塗布契約テスト用に必須引数を補い、paths を list で返す。"""
    kwargs.setdefault("dispense_mode", "area")
    kwargs.setdefault("auto_line_aspect_ratio", _AUTO_LINE_ASPECT_RATIO)
    kwargs.setdefault("auto_area_short_side_factor", _AUTO_AREA_SHORT_SIDE_FACTOR)
    plan = FillPlan.build(polygon, nozzle_diameter, **kwargs)
    return [list(path) for path in plan.paths]


def _config(nozzle_diameter: float) -> PasteDispenserConfig:
    return PasteDispenserConfig(
        rotations_per_ul=45.0,
        nozzle_diameter=nozzle_diameter,
        max_fill_speed=2.0,
        max_dispense_rate=5.0,
        dispense_accel=10.0,
        retract_amount=10.0,
        retract_rate=10.0,
        retract_accel_factor=2.0,
        toolhead=Toolhead(x=0.0, y=0.0),
        paste_height=0.5,
        ul_per_mm2=0.05,
        auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
        auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
    )


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
    """点塗布に落ちたときの点の位置契約.

    面 / 線 / 点 の段の誘発自体は ``TestDispenseModes`` と ``TestAreaFill`` が押さえる。
    """

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
        plan = FillPlan.build(
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
        plan = FillPlan.build(
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
        line_plan = FillPlan.build(
            _rectangle(0.8, 5.0),
            nozzle_diameter=1.0,
            dispense_mode="area",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AUTO_AREA_SHORT_SIDE_FACTOR,
        )
        dot_plan = FillPlan.build(
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

        plan = FillPlan.build(
            rotated,
            nozzle_diameter=0.34,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=_AREA_DISABLED_FACTOR,
        )

        assert plan.dispense_mode == "line"

    @pytest.mark.parametrize(
        ("width", "height", "aspect_ratio", "area_factor", "expected_mode"),
        [
            # 面塗布を無効化した上で、縦横比だけで line / dot が決まる
            (1.0, 1.6, _AUTO_LINE_ASPECT_RATIO, _AREA_DISABLED_FACTOR, "dot"),
            (1.0, 1.7, _AUTO_LINE_ASPECT_RATIO, _AREA_DISABLED_FACTOR, "line"),
            (1.0, 1.0, _AUTO_LINE_ASPECT_RATIO, _AREA_DISABLED_FACTOR, "dot"),
            # 縦横比がちょうど閾値なら dot（厳密 > で判定する）
            (1.0, 2.0, 2.0, _AREA_DISABLED_FACTOR, "dot"),
            # 短辺 < 面塗布閾値 0.34*3 なら、面塗布を有効にしても line / dot へ落ちる
            (0.51, 4.08, _AUTO_LINE_ASPECT_RATIO, 3.0, "line"),
            (0.51, 0.612, _AUTO_LINE_ASPECT_RATIO, 3.0, "dot"),
        ],
    )
    def test_auto_resolves_line_and_dot_by_aspect_ratio(
        self, width, height, aspect_ratio, area_factor, expected_mode
    ):
        plan = FillPlan.build(
            _rectangle(width, height),
            nozzle_diameter=0.34,
            dispense_mode="auto",
            auto_line_aspect_ratio=aspect_ratio,
            auto_area_short_side_factor=area_factor,
        )

        assert plan.dispense_mode == expected_mode

    @pytest.mark.parametrize(
        ("short_delta", "expected_mode"),
        [
            (0.1, "area"),  # 閾値の直上 → area（aspect も成立するが area が勝つ）
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

        plan = FillPlan.build(
            _rectangle(short, long),
            nozzle_diameter=nozzle_diameter,
            dispense_mode="auto",
            auto_line_aspect_ratio=_AUTO_LINE_ASPECT_RATIO,
            auto_area_short_side_factor=factor,
        )

        assert plan.dispense_mode == expected_mode

    def test_auto_degenerate_sliver_resolves_without_error(self):
        # ほぼ退化した極薄スライバでも auto がゼロ除算せず解決する（防御的契約）。
        sliver = Polygon([(0, 0), (5, 0), (5, 1e-9), (0, 1e-9)])

        plan = FillPlan.build(
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
        # Arrange: buffer(-inset) が面として残る十分大きな矩形
        polygon = _rectangle(width, length)
        assert not polygon.buffer(-nozzle_diameter / 2).is_empty  # 前提: 面が残る

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


class TestCoverage:
    """Overlap を上げると行間隔 line_spacing = w*(1-overlap) が縮む.

    しきいは line_spacing の式から導出する。
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


class TestExteriorMargin:
    """boundary_margin>0 で全頂点が外周から margin 以上内側."""

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
    """空 / 不正ポリゴンは空の計画になる（値の検証は params / config 側の責務）."""

    def test_empty_polygon_returns_empty_list(self):
        result = build_paste_fill_path(Polygon(), nozzle_diameter=1.0)

        assert result == []

    def test_invalid_polygon_returns_empty_list(self):
        # 自己交差する不正ポリゴン（蝶ネクタイ）
        invalid = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
        assert not invalid.is_valid  # 前提: 不正形状

        result = build_paste_fill_path(invalid, nozzle_diameter=1.0)

        assert result == []


class TestFillPlanForPad:
    """FillPlan.for_pad は config + PasteParams から引数対応を単一ソース化する。

    プレビュー（webui router）と実行（PasteApplicator）が同一の対応で FillPlan.build
    を呼ぶための束ねメソッド。line_direction の解決が固有の振る舞い。
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

    @pytest.mark.parametrize("dispense_mode", ["line", "auto", "area"])
    def test_outward_starts_near_component_for_every_line_resolution(
        self, dispense_mode: str
    ):
        reference = Point2d(0.4, -5.0)

        plan = FillPlan.for_pad(
            _rectangle(0.8, 5.0),
            config=_config(1.0),
            params=self._paste(
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
        kwargs = {"config": _config(1.0), "line_reference": reference}

        outward = FillPlan.for_pad(
            polygon,
            params=self._paste(
                dispense_mode="line",
                line_direction="outward",
                boundary_margin=0.0,
            ),
            **kwargs,
        )
        inward = FillPlan.for_pad(
            polygon,
            params=self._paste(
                dispense_mode="line",
                line_direction="inward",
                boundary_margin=0.0,
            ),
            **kwargs,
        )

        assert inward.paths[0] == tuple(reversed(outward.paths[0]))

    def test_directional_line_without_reference_is_unconstrained(self):
        params = self._paste(dispense_mode="line", line_direction="outward")
        unconstrained = self._paste(
            dispense_mode="line", line_direction="unconstrained"
        )

        plan = FillPlan.for_pad(
            _rectangle(0.8, 5.0), config=_config(1.0), params=params
        )

        assert plan == FillPlan.for_pad(
            _rectangle(0.8, 5.0), config=_config(1.0), params=unconstrained
        )

    def test_equal_distance_keeps_unconstrained_order(self):
        polygon = _rectangle(0.8, 5.0)
        reference = Point2d(0.4, 2.5)
        unconstrained = FillPlan.for_pad(
            polygon,
            config=_config(1.0),
            params=self._paste(
                dispense_mode="line",
                line_direction="unconstrained",
                boundary_margin=0.0,
            ),
        )
        outward = FillPlan.for_pad(
            polygon,
            config=_config(1.0),
            params=self._paste(
                dispense_mode="line",
                line_direction="outward",
                boundary_margin=0.0,
            ),
            line_reference=reference,
        )

        assert outward.paths == unconstrained.paths


class TestComponentAmount:
    """成分 1 本あたりの塗布量（塗布実行と所要時間見積りが共有する配分）."""

    @staticmethod
    def _paste(ul_per_mm2: float = 0.1) -> PasteParams:
        return PasteParams(
            dispense_mode="area",
            line_direction="unconstrained",
            paste_height=0.05,
            ul_per_mm2=ul_per_mm2,
            prime_extra_delay=0.0,
            bead_width_factor=0.8,
            overlap=0.2,
            boundary_margin=0.05,
        )

    def test_splits_the_pad_amount_evenly_across_components(self):
        # 分割された 2 成分。総量 = 面積 * ul_per_mm2 を成分数で等分する。
        polygon = _split_dumbbell_h()
        params = self._paste(ul_per_mm2=0.1)
        plan = FillPlan.for_pad(polygon, config=_config(0.4), params=params)
        assert len(plan.paths) == 2  # 前提: 2 成分に割れている

        amount = plan.component_amount_ul(polygon, params)

        assert amount * len(plan.paths) == pytest.approx(polygon.area * 0.1)

    def test_empty_plan_has_no_amount(self):
        params = self._paste()
        plan = FillPlan.for_pad(Polygon(), config=_config(0.4), params=params)

        assert plan.component_amount_ul(Polygon(), params) == 0.0
