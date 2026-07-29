"""Posctrl/region の仕様テスト.

計画書 memory/agents/implementation-planner/region-alignment-average.md
「公開インターフェース → src/pcbasm/posctrl/region.py」に基づく。

plan_alignment_regions は撮像もステージ移動もしない純幾何関数。想定エッジの
線分（pixel 空間へ投影済み）から拘束行列 A = Σ L·n nᵀ を積み、その最小固有値
λ_min を「最も弱く拘束されている方向の拘束量」として候補を採点する。これ 1 本で
「エッジ量が十分」と「x/y 両方向に拘束がある」を同時に表すので、一方向エッジ
だけの領域（開口問題）は λ_min = 0 で自動的に落ちる。

予測 sharpness = sqrt(constraint / edge_length_px) は照合後の実測 sharpness
（CopperEdgeMatcher）と同じ量で、等方な正方リングでは sqrt(1/2) = 0.707。
"""

import pytest
import shapely
from shapely import affinity

from pcbasm.geometry import Identity, Point2d, Rotation, Transform
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
    pad_centers: list[Point2d],
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
        pad_centers,
        transform,
        region_size_px=region_size_px,
        count=count,
        image_size=image_size,
        tour_start=tour_start,
    )


class TestPlanAlignmentRegionsScoring:
    """λ_min による採点（拘束の強い領域が選ばれる）."""

    def test_isotropic_copper_region_is_planned_with_high_constraint(self):
        """両方向に拘束のある正方リングの領域は予測 sharpness ≈ 0.707.

        pad が 1 個なら候補格子も 1 点なので、その 1 領域が採点される。
        """
        regions = _plan([_square(0.0, 0.0, 3.0)], [Point2d(0.0, 0.0)])

        assert len(regions) == 1
        assert regions[0].constraint > 0.0
        assert regions[0].predicted_sharpness > 0.5

    def test_one_directional_copper_yields_no_region(self):
        """水平エッジしか無い銅箔は λ_min = 0 なので候補が全滅する.

        開口問題の領域を計画段階で落とす。この銅箔の法線は (0, ±1) だけなので A = [[0, 0], [0, ΣL]]
        となり λ_min は厳密に 0 になる。
        """
        regions = _plan(
            [_horizontal_strip(-60.0, 60.0)], [Point2d(-20.0, 0.0), Point2d(20.0, 0.0)]
        )

        assert regions == []

    def test_isotropic_region_is_preferred_over_one_directional(self):
        """1 領域だけ選ぶなら一方向エッジ側ではなく等方な銅箔側を取る."""
        polygons = [_square(-15.0, 0.0, 3.0), _horizontal_strip(10.0, 60.0)]

        regions = _plan(polygons, [Point2d(-15.0, 0.0), Point2d(20.0, 0.0)], count=1)

        assert len(regions) == 1
        assert regions[0].anchor.x == pytest.approx(-15.0, abs=REGION_MM / 2)
        assert regions[0].predicted_sharpness > 0.5

    def test_no_copper_yields_empty_list(self):
        """銅箔が無ければ空リスト（不足でも例外は投げない）."""
        assert _plan([], [Point2d(0.0, 0.0)]) == []


class TestPlanAlignmentRegionsSelection:
    """貪欲選択・領域数の上限・巡回順."""

    POLYGONS = [
        _square(-15.0, 0.0, 3.0),
        _square(0.0, 0.0, 3.0),
        _square(15.0, 0.0, 3.0),
    ]
    PADS = [Point2d(-15.0, 0.0), Point2d(0.0, 0.0), Point2d(15.0, 0.0)]

    def test_selected_regions_are_separated_by_at_least_one_region_size(self):
        """選ばれた領域どうしは board 上で region_size 以上離れている."""
        regions = _plan(self.POLYGONS, self.PADS, count=3)

        assert len(regions) == 3
        anchors = [r.anchor for r in regions]
        for i, a in enumerate(anchors):
            for b in anchors[i + 1 :]:
                assert (a - b).norm >= REGION_MM - 1e-6

    def test_count_caps_the_number_of_regions(self):
        """Count を超えて選ばない."""
        regions = _plan(self.POLYGONS, self.PADS, count=2)

        assert len(regions) == 2

    def test_fewer_candidates_than_count_returns_what_is_available(self):
        """候補が count に満たなければ候補数まで返す（例外は投げない）."""
        regions = _plan([_square(0.0, 0.0, 3.0)], [Point2d(0.0, 0.0)], count=4)

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
        pads = [Point2d(-15.0, 0.0), Point2d(15.0, 0.0)]

        regions = _plan(polygons, pads, count=2, tour_start=tour_start)

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
            [Point2d(-15.0, 0.0), Point2d(15.0, 0.0)],
            count=2,
        )

        assert len(regions) == 2
        assert {r.roi for r in regions} == {centered_roi(IMAGE_SIZE, REGION_PX)}

    def test_anchor_is_the_board_point_mapped_to_machine_coordinates(self):
        """Anchor = board_transform.apply(領域中心の board 点)."""
        board_transform = Rotation(30.0)

        regions = _plan(
            [_square(0.0, 0.0, 3.0)],
            [Point2d(4.0, 2.0)],
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
        board_transform = Rotation(30.0)

        regions = _plan(
            [_square(0.0, 0.0, 3.0)],
            [Point2d(0.0, 0.0)],
            board_transform=board_transform,
        )

        assert len(regions) == 1
        assert regions[0].roi == centered_roi(IMAGE_SIZE, REGION_PX)
        assert regions[0].predicted_sharpness > 0.5


class TestPredictedSharpnessMatchesMeasured:
    """幾何で予測した sharpness と、照合が実測する sharpness が同一スケールにあること.

    領域選定（λ_min / エッジ総長）と照合の棄却（3x3 コスト近傍のヘッセ）は
    別々に正規化されているが、同じ量を測っている前提で組み合わせている。 どちらかの正規化が変わると乖離するだけで両者とも動き続け、その結果
    「計画段階では拘束十分に見えるのに照合が棄却する（またはその逆）」という
    静かな破綻になる。予測は理想線分に対する値なのでラスタライズの階段状に よる劣化を含まず、**わずかに楽観側（予測 >= 実測）**
    に出る。 実 PCB TJ-56-67 の 400px 領域では予測 0.633〜0.682 / 実測 0.595〜0.662。
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
        """予測 sqrt(constraint / edge_length_px) と実測 sharpness が近い値になる.

        正規化を片方だけ変える（例: 照合側の 2n を n にする、予測側の sqrt を 落とす）と 1.4〜2
        倍ずれてこの許容を外れる。
        """
        projector = _projector(polygons)
        regions = plan_alignment_regions(
            projector,
            [Point2d(0.0, 0.0)],
            Identity(),
            region_size_px=200,
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
            _plan([_square(0.0, 0.0, 3.0)], [Point2d(0.0, 0.0)], region_size_px=0)

    def test_count_below_one_raises(self):
        with pytest.raises(ValueError, match="count"):
            _plan([_square(0.0, 0.0, 3.0)], [Point2d(0.0, 0.0)], count=0)

    def test_empty_pad_centers_raises(self):
        with pytest.raises(ValueError, match="pad_centers"):
            _plan([_square(0.0, 0.0, 3.0)], [])

    def test_region_larger_than_the_frame_raises(self):
        with pytest.raises(ValueError, match="region_size_px"):
            _plan(
                [_square(0.0, 0.0, 3.0)],
                [Point2d(0.0, 0.0)],
                region_size_px=min(IMAGE_SIZE) + 1,
            )
