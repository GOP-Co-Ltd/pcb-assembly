"""Posctrl/region の仕様テスト.

計画書 memory/agents/implementation-planner/region-alignment-average.md
「公開インターフェース → src/pcbasm/posctrl/region.py」に基づく。

plan_alignment_regions は撮像もステージ移動もしない純幾何関数。想定エッジを
約 1px 間隔の点に落とし、ROI 内に入った点の単位法線から拘束行列 A = Σ n nᵀ を
積み、その最小固有値 λ_min を「最も弱く拘束されている方向の拘束量」として
候補を採点する。これ 1 本で「エッジ量が十分」と「x/y 両方向に拘束がある」を
同時に表すので、一方向エッジだけの領域（開口問題）は λ_min = 0 で自動的に落ちる。

候補にできるのは **ROI 全体が safe_area に収まる位置だけ**。safe_area は基板外形を
外周マージンだけ内側へ縮めた図形で、領域選定の定義域と外周除外をこの 1 つの
引数が兼ねる（TestBoardEdgeMargin がユーザー要求「外周部でマッチしてはいけない」
の直接ピン）。

予測 sharpness = sqrt(constraint / edge_point_count) は照合後の実測 sharpness
（CopperEdgeMatcher）と同じ量で、等方な正方リングでは sqrt(1/2) = 0.707。
"""

import pytest
import shapely
from shapely import affinity

from pcbasm.geometry import Identity, Point2d, Rotation, Shift, Transform
from pcbasm.posctrl import (
    AlignmentRegion,
    CopperEdgeMatcher,
    CopperProjector,
    centered_roi,
    plan_alignment_regions,
)

PPM = 10.0  # pixel/mm
IMAGE_SIZE = (400, 400)
REGION_PX = 100  # = 10mm @ 10 px/mm
REGION_MM = REGION_PX / PPM

# 候補格子は safe_area の bbox に REGION_MM/2 間隔で張られ、ROI 全体が safe_area に
# 収まる格子点だけが候補になる。横長のこの矩形では x が ±20mm・y が ±5mm まで候補。
WIDE_AREA = shapely.box(-25.0, -10.0, 25.0, 10.0)


def _square(cx: float, cy: float, half: float) -> shapely.Polygon:
    """中心 (cx, cy)・半幅 half の正方形ポリゴン (mm, board 座標)."""
    return shapely.Polygon(
        [
            (cx - half, cy - half),
            (cx + half, cy - half),
            (cx + half, cy + half),
            (cx - half, cy + half),
        ]
    )


def _horizontal_strip(x0: float, x1: float) -> shapely.Polygon:
    """Y 方向にしか法線を持たない細長い銅箔（水平エッジのみ）."""
    return shapely.Polygon([(x0, -0.25), (x1, -0.25), (x1, 0.25), (x0, 0.25)])


def _solo_area(cx: float, cy: float, region_mm: float = REGION_MM) -> shapely.Polygon:
    """候補を (cx, cy) の 1 点だけに絞る safe_area（board 座標、mm）.

    半径 0.9*region_mm の円。中心の ROI（対角半径 0.707*region_mm）は収まるが、 格子間隔
    0.45*region_mm だけ離れた隣接点の ROI は必ず隅が円外に出る。 2r/step = 3.6
    が整数から離れているので、bbox の格子が中心を通ることが region_mm
    の丸め誤差に左右されない（board_transform に回転があっても同じ）。
    """
    return shapely.Point(cx, cy).buffer(0.9 * region_mm)


def _projector(
    polygons: list[shapely.Polygon], board_transform: Transform | None = None
) -> CopperProjector:
    return CopperProjector(
        polygons=polygons,
        board_transform=board_transform if board_transform is not None else Identity(),
        offset_transform=Identity(),
        pixel_per_mm=PPM,
        image_size=IMAGE_SIZE,
    )


def _plan(
    polygons: list[shapely.Polygon],
    safe_area: shapely.Polygon,
    *,
    count: int = 4,
    board_transform: Transform | None = None,
    region_size_px: int = REGION_PX,
    image_size: tuple[int, int] = IMAGE_SIZE,
    tour_start: Point2d = Point2d(0.0, 0.0),
) -> list[AlignmentRegion]:
    transform = board_transform if board_transform is not None else Identity()
    return plan_alignment_regions(
        _projector(polygons, transform),
        transform,
        safe_area=safe_area,
        region_size_px=region_size_px,
        count=count,
        image_size=image_size,
        tour_start=tour_start,
    )


def _roi_in_board(
    region: AlignmentRegion, board_transform: Transform
) -> shapely.Polygon:
    """領域の ROI を board 座標の多角形へ写す（回転があれば軸平行にならない）.

    投影公式 ``pixel = 画像中心 + pixel_per_mm * (stage − T_b(b))``（offset 変換は
    Identity）より、anchor へ移動したときの ROI は機械座標では anchor 中心・一辺
    ``region_size_px / pixel_per_mm`` の軸平行正方形。board 座標へは T_b の逆で戻す。
    """
    half = REGION_MM / 2
    inverse = board_transform.inverse()
    corners = [
        inverse.apply(Point2d(region.anchor.x + sx * half, region.anchor.y + sy * half))
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
    ]
    return shapely.Polygon([(c.x, c.y) for c in corners])


class TestPlanAlignmentRegionsScoring:
    """λ_min による採点（拘束の強い領域が選ばれる）."""

    def test_isotropic_copper_region_is_planned_with_high_constraint(self):
        """両方向に拘束のある正方リングの領域は予測 sharpness ≈ 0.707."""
        regions = _plan([_square(0.0, 0.0, 3.0)], _solo_area(0.0, 0.0))

        assert len(regions) == 1
        assert regions[0].constraint > 0.0
        assert regions[0].predicted_sharpness > 0.5

    def test_one_directional_copper_yields_no_region(self):
        """水平エッジしか無い銅箔は λ_min = 0 なので候補が全滅する.

        開口問題の領域を計画段階で落とす。この銅箔の法線は (0, ±1) だけなので A = [[0, 0], [0, n]] となり
        λ_min は厳密に 0 になる。
        """
        regions = _plan([_horizontal_strip(-60.0, 60.0)], WIDE_AREA)

        assert regions == []

    def test_isotropic_region_is_preferred_over_one_directional(self):
        """1 領域だけ選ぶなら一方向エッジ側ではなく等方な銅箔側を取る."""
        polygons = [_square(-15.0, 0.0, 3.0), _horizontal_strip(10.0, 60.0)]

        regions = _plan(polygons, WIDE_AREA, count=1)

        assert len(regions) == 1
        assert regions[0].anchor.x == pytest.approx(-15.0, abs=REGION_MM / 2)
        assert regions[0].predicted_sharpness > 0.5

    def test_no_copper_yields_empty_list(self):
        """銅箔が無ければ空リスト（不足でも例外は投げない）."""
        assert _plan([], _solo_area(0.0, 0.0)) == []


class TestPlanAlignmentRegionsSelection:
    """貪欲選択・領域数の上限・巡回順."""

    POLYGONS = [
        _square(-15.0, 0.0, 3.0),
        _square(0.0, 0.0, 3.0),
        _square(15.0, 0.0, 3.0),
    ]

    def test_selected_regions_are_separated_by_at_least_one_region_size(self):
        """選ばれた領域どうしは board 上で region_size 以上離れている."""
        regions = _plan(self.POLYGONS, WIDE_AREA, count=3)

        assert len(regions) == 3
        anchors = [r.anchor for r in regions]
        for i, a in enumerate(anchors):
            for b in anchors[i + 1 :]:
                assert (a - b).norm >= REGION_MM - 1e-6

    def test_count_caps_the_number_of_regions(self):
        """Count を超えて選ばない."""
        regions = _plan(self.POLYGONS, WIDE_AREA, count=2)

        assert len(regions) == 2

    def test_fewer_candidates_than_count_returns_what_is_available(self):
        """候補が count に満たなければ候補数まで返す（例外は投げない）.

        銅箔が 1 島だけなら、周囲の格子点は全てその島から region_size 以内なので 貪欲選択が 1 領域で打ち切る。
        """
        regions = _plan([_square(0.0, 0.0, 3.0)], WIDE_AREA, count=4)

        assert len(regions) == 1

    @pytest.mark.parametrize(
        ("tour_start", "first_x"),
        [(Point2d(-40.0, 0.0), -15.0), (Point2d(40.0, 0.0), 15.0)],
    )
    def test_regions_are_ordered_from_the_tour_start(
        self, tour_start: Point2d, first_x: float
    ):
        """巡回起点に近い領域が先頭に来る（index は巡回順に 0 から振り直す）."""
        polygons = [_square(-15.0, 0.0, 3.0), _square(15.0, 0.0, 3.0)]

        regions = _plan(polygons, WIDE_AREA, count=2, tour_start=tour_start)

        assert len(regions) == 2
        assert [r.index for r in regions] == [0, 1]
        assert regions[0].anchor.x == pytest.approx(first_x, abs=REGION_MM / 2)


class TestPlanAlignmentRegionsRoiAndAnchor:
    """ROI と anchor の契約."""

    def test_roi_is_the_same_image_centered_square_for_every_region(self):
        """全 region の roi が画像中心の region_size_px 正方形で同一.

        アンカーへ移動すると対象領域が画像中心へ来るので、ROI は固定でよい。
        """
        regions = _plan(
            [_square(-15.0, 0.0, 3.0), _square(15.0, 0.0, 3.0)],
            WIDE_AREA,
            count=2,
        )

        assert len(regions) == 2
        assert {r.roi for r in regions} == {centered_roi(IMAGE_SIZE, REGION_PX)}

    @pytest.mark.parametrize(
        "board_transform", [Shift(5.0, -1.0), Rotation(30.0), Rotation(-45.0)]
    )
    def test_anchor_is_the_board_point_mapped_to_machine_coordinates(
        self, board_transform: Transform
    ):
        """Anchor = board_transform.apply(領域中心の board 点).

        safe_area で候補を board (4, 2) の 1 点に絞り、回転（線形部）と並進の どちらも anchor
        に反映されることを見る。
        """
        regions = _plan(
            [_square(0.0, 0.0, 3.0)],
            _solo_area(4.0, 2.0),
            board_transform=board_transform,
        )

        assert len(regions) == 1
        want = board_transform.apply(Point2d(4.0, 2.0))
        assert regions[0].anchor.x == pytest.approx(want.x, abs=1e-6)
        assert regions[0].anchor.y == pytest.approx(want.y, abs=1e-6)

    def test_rotated_board_transform_keeps_the_roi_an_image_aligned_square(self):
        """board_transform に回転が入っても ROI は pixel 空間の正方形のまま.

        採点も pixel 空間で行うので、board 空間の正方形が画像上で正方形に ならない回転下でも領域と ROI
        が食い違わない。
        """
        regions = _plan(
            [_square(0.0, 0.0, 3.0)],
            _solo_area(0.0, 0.0),
            board_transform=Rotation(30.0),
        )

        assert len(regions) == 1
        assert regions[0].roi == centered_roi(IMAGE_SIZE, REGION_PX)
        assert regions[0].predicted_sharpness > 0.5


class TestBoardEdgeMargin:
    """外周マージン: ROI が基板の外周部に掛かる位置は候補にしない.

    ユーザー要求「ボードの外周部はやすり掛けなどで物理的に破壊されやすい領域で、 ここでマッチしてはいけません。できれば 1〜2mm
    ほど内側の銅箔で合わせるべき」 の直接ピン。呼び出し側は safe_area =
    基板外形.buffer(-board_edge_margin) を 渡すだけでよい。

    ROI の中心ではなく **ROI 全体**を内側へ入れるのは、削れた銅箔を避けるため
    だけでなく、基板外形そのものの強いエッジを視野に入れないため。外形線は CopperProjector
    が描く想定エッジに一切含まれないので、視野に入ると 片方向 chamfer では一切ペナルティを受けない偽エッジとして働く。
    """

    OUTLINE = shapely.box(0.0, 0.0, 60.0, 40.0)
    # 外形の隅から 1mm の位置まで銅箔を敷く。外周寄りの候補が「銅箔が無いから」
    # ではなく「ROI が外周に掛かるから」落ちることを見るための地形。
    COPPER = [
        _square(x + 2.5, y + 2.5, 1.5) for x in range(0, 60, 5) for y in range(0, 40, 5)
    ]

    @pytest.mark.parametrize("margin", [0.0, 1.0, 2.0, 3.0])
    @pytest.mark.parametrize("board_transform", [Identity(), Rotation(20.0)])
    def test_every_selected_roi_stays_inside_the_shrunk_outline(
        self, margin: float, board_transform: Transform
    ):
        """選ばれた全領域の ROI が board 座標で外形.buffer(-margin) に収まる.

        board_transform に回転があると ROI は board 空間で軸平行にならないので、 「中心が内側」では足りず
        4 隅すべてを見る必要がある。
        """
        safe_area = self.OUTLINE.buffer(-margin)

        regions = _plan(self.COPPER, safe_area, board_transform=board_transform)

        assert regions, "銅箔を敷き詰めた基板なので領域は選ばれるはず（空振り防止）"
        for region in regions:
            roi = _roi_in_board(region, board_transform)
            assert roi.within(safe_area.buffer(1e-9)), region.anchor

    def test_margin_keeps_the_roi_away_from_the_board_edge(self):
        """Margin を 0 → 3mm にすると ROI が外形線から 3mm 以上離れる.

        margin = 0 では外形に接する領域が実際に選ばれる（＝マージンが効いている
        ことの対偶）。この距離差がユーザー要求そのもの。
        """
        no_margin = _plan(self.COPPER, self.OUTLINE)
        with_margin = _plan(self.COPPER, self.OUTLINE.buffer(-3.0))

        def closest_edge_distance(regions: list[AlignmentRegion]) -> float:
            return min(
                self.OUTLINE.exterior.distance(_roi_in_board(r, Identity()))
                for r in regions
            )

        assert closest_edge_distance(no_margin) < 0.5
        assert closest_edge_distance(with_margin) >= 3.0
        assert {(r.anchor.x, r.anchor.y) for r in no_margin} != {
            (r.anchor.x, r.anchor.y) for r in with_margin
        }

    def test_margin_larger_than_the_board_yields_no_regions(self):
        """縮めた外形が空（マージンが基板より大きい）なら領域 0 個・例外なし.

        中止するかどうかは min_regions を持つ呼び出し側の責務。
        """
        safe_area = self.OUTLINE.buffer(-25.0)

        assert safe_area.is_empty
        assert _plan(self.COPPER, safe_area) == []

    def test_safe_area_smaller_than_the_roi_yields_no_regions(self):
        """ROI が収まる格子点が 1 つも無ければ空リスト（例外は投げない）."""
        assert _plan([_square(0.0, 0.0, 3.0)], _square(0.0, 0.0, 3.0)) == []


class TestPredictedSharpnessMatchesMeasured:
    """幾何で予測した sharpness と、照合が実測する sharpness が同一スケールにあること.

    領域選定（λ_min / エッジ点数）と照合の棄却（3x3 コスト近傍のヘッセ）は
    別々に正規化されているが、同じ量を測っている前提で組み合わせている。 どちらかの正規化が変わると乖離するだけで両者とも動き続け、その結果
    「計画段階では拘束十分に見えるのに照合が棄却する（またはその逆）」という
    静かな破綻になる。予測は理想線分に対する値なのでラスタライズの階段状に よる劣化を含まず、**わずかに楽観側（予測 >= 実測）**
    に出る。 実 PCB TJ-56-67 の 400px 領域（外周マージン 2mm）では予測 0.649〜0.681 で、 予測 /
    実測は 1.030〜1.120。斜めエッジが支配的な形状では距離変換が Bresenham の階段までの距離を測るので比が最大
    1.225 まで開く（点サンプリングを 細かくしても縮まらない：sqrt(λ_min/点数) はサンプリング密度に不変）。
    """

    @staticmethod
    def _measured_sharpness(
        projector: CopperProjector, region: AlignmentRegion
    ) -> float:
        """領域のアンカーで投影した想定エッジ自身を照合したときの sharpness."""
        projection = projector.project(region.anchor)
        matcher = CopperEdgeMatcher(pixel_per_mm=PPM, min_sharpness=0.0)
        match = matcher.match(projection.edge_mask, projection.edge_mask, region.roi)
        assert match is not None
        return match.sharpness

    @pytest.mark.parametrize(
        ("label", "polygons", "tolerance"),
        [
            # 軸平行の正方リング: 等方な上限ケース。予測 sqrt(1/2) = 0.7071
            ("axis_aligned_ring", [_square(0.0, 0.0, 6.0)], 0.05),
            # 向きの異なる矩形の集まり（実 PCB に近い雑多さ）
            (
                "mixed_orientations",
                [
                    _square(-6.0, -6.0, 2.5),
                    affinity.rotate(_square(6.0, 5.0, 3.0), 30.0),
                    _square(0.0, 8.0, 1.5),
                    affinity.rotate(_square(7.0, -7.0, 2.0), 60.0),
                ],
                0.15,
            ),
        ],
    )
    def test_predicted_and_measured_sharpness_agree(
        self, label: str, polygons: list[shapely.Polygon], tolerance: float
    ):
        """予測 sqrt(constraint / edge_point_count) と実測 sharpness が近い値になる.

        正規化を片方だけ変える（例: 照合側の 2n を n にする、予測側の sqrt を 落とす）と 1.4〜2
        倍ずれてこの許容を外れる。
        """
        region_size_px = 200
        projector = _projector(polygons)
        regions = plan_alignment_regions(
            projector,
            Identity(),
            safe_area=_solo_area(0.0, 0.0, region_size_px / PPM),
            region_size_px=region_size_px,
            count=1,
            image_size=IMAGE_SIZE,
            tour_start=Point2d(0.0, 0.0),
        )

        assert len(regions) == 1, label
        predicted = regions[0].predicted_sharpness
        measured = self._measured_sharpness(projector, regions[0])

        assert predicted > 0.5  # どちらも採択側（閾値 0.15 から十分離れている）
        assert measured > 0.5
        assert measured == pytest.approx(predicted, rel=tolerance)
        # ラスタライズの劣化分だけ予測が楽観側に出る（逆向きには外れない）
        assert predicted >= measured - 1e-9


class TestPlanAlignmentRegionsValidation:
    """引数検証（いずれも ValueError）."""

    def test_region_size_below_one_raises(self):
        with pytest.raises(ValueError, match="region_size_px"):
            _plan([_square(0.0, 0.0, 3.0)], WIDE_AREA, region_size_px=0)

    def test_count_below_one_raises(self):
        with pytest.raises(ValueError, match="count"):
            _plan([_square(0.0, 0.0, 3.0)], WIDE_AREA, count=0)

    def test_region_larger_than_the_frame_raises(self):
        with pytest.raises(ValueError, match="region_size_px"):
            _plan(
                [_square(0.0, 0.0, 3.0)],
                WIDE_AREA,
                region_size_px=min(IMAGE_SIZE) + 1,
            )
