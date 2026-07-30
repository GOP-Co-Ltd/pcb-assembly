"""Posctrl/region の仕様テスト.

region-local-correction（区ごとの局所補正）の仕様に基づく。

plan_alignment_regions は撮像もステージ移動もしない純幾何関数。領域は
**pixel 空間に張った重なりなしのタイル**で、位相は塗布対象 pad 中心の重心に
合わせる（safe_area の bbox 端に合わせると board_transform の回転で位相が動き、
実測で計画区数が θ=0° 9 → 0.5° 4 → 2° 3 と暴れた）。

``AlignmentRegion.board_center`` は区中心の board 座標。タイルが**等サイズの
正方格子**なので「board 座標で最も近い区中心」＝「その点を含む区」になり、
``BoardAlignment.correction_for`` は包含判定とフォールバックの場合分けを持たずに
距離最小の 1 規則だけで pad ごとの補正を引ける。この格子性が本ファイルの
中心的なピン（``TestBoardCenter``）。

採否は 3 条件だけ:

1. ROI 全体が safe_area に収まる（safe_area = 基板外形を外周マージンだけ内側へ
   縮めた図形。ユーザー要求「外周部でマッチしてはいけない」の直接ピン）
2. 区内に塗布対象 pad の中心が 1 つ以上ある
3. 予測 sharpness >= min_sharpness（一方向エッジだけの区を移動前に落とす保険）

区数の上限は無い（旧 count / 貪欲選択 / 最小離間は撤去）。条件を満たす区は
全部使い、それぞれが自分の区内の pad の補正になる。

予測 sharpness = sqrt(constraint / edge_point_count) は照合後の実測 sharpness
（CopperEdgeMatcher）と同じ量で、等方な正方リングでは sqrt(1/2) = 0.707。
"""

import math

import numpy as np
import pytest
import shapely
from shapely import affinity

from pcbasm.geometry import Identity, Matrix2d, Point2d, Rotation, Shift, Transform
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

# 横長の safe_area。ROI (10mm 角) 全体が収まるのは x |x| <= 20 / y |y| <= 5 の帯
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
    """タイルを (cx, cy) 中心の 1 枚だけに絞る safe_area（board 座標、mm）.

    半径 0.9*region_mm の円。中心のタイルの ROI（対角半径 0.707*region_mm）は
    収まるが、region_mm 離れた隣のタイルは中心がすでに円外なので必ず落ちる。
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
    pad_centers: list[Point2d],
    safe_area: shapely.Polygon,
    *,
    board_transform: Transform | None = None,
    region_size_px: int = REGION_PX,
    min_sharpness: float = 0.0,
    image_size: tuple[int, int] = IMAGE_SIZE,
    tour_start: Point2d = Point2d(0.0, 0.0),
) -> list[AlignmentRegion]:
    transform = board_transform if board_transform is not None else Identity()
    return plan_alignment_regions(
        _projector(polygons, transform),
        transform,
        pad_centers,
        safe_area=safe_area,
        region_size_px=region_size_px,
        min_sharpness=min_sharpness,
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


def _nearest_region(
    regions: list[AlignmentRegion], board_point: Point2d
) -> AlignmentRegion:
    """Board 座標で最も近い区中心を持つ領域（correction_for のルックアップ規則）."""
    return min(regions, key=lambda region: (region.board_center - board_point).norm)


def _grid_pads(
    xs: range | tuple[float, ...], ys: range | tuple[float, ...]
) -> list[Point2d]:
    return [Point2d(float(x), float(y)) for x in xs for y in ys]


class TestTiling:
    """重なりなしのタイル張り（旧・貪欲選択 + 最小離間の置き換え）."""

    # pad を 10mm 格子（= region_mm）に並べ、各 pad の位置に等方な銅箔を置く。
    # 位相が pad 重心に合うので、タイル中心はそのまま pad 中心に一致する。
    PADS = _grid_pads(range(-40, 41, 10), (-10.0, 0.0, 10.0))
    COPPER = [_square(p.x, p.y, 3.0) for p in PADS]
    AREA = shapely.box(-60.0, -30.0, 60.0, 30.0)

    def test_tiles_do_not_overlap(self):
        """どの 2 区も一辺 region_size_px の正方形として重ならない.

        board_transform が相似写像（Identity / 回転）なので、board 座標での 中心間距離が
        region_mm 以上あれば pixel 空間で重なっていない。
        """
        regions = _plan(self.COPPER, self.PADS, self.AREA)

        assert len(regions) >= 10
        anchors = [r.anchor for r in regions]
        for index, a in enumerate(anchors):
            for b in anchors[index + 1 :]:
                assert max(abs(a.x - b.x), abs(a.y - b.y)) >= REGION_MM - 1e-6

    def test_every_qualifying_tile_is_returned_without_a_cap(self):
        """条件を満たす区は上限なく全部返る（旧 count が無いことのピン）.

        局所補正では区の数がそのまま補正の空間分解能になるので、区を落とす理由は 3 条件以外に無い。
        """
        regions = _plan(self.COPPER, self.PADS, self.AREA)

        assert len(regions) == len(self.PADS)
        assert [r.index for r in regions] == list(range(len(regions)))

    def test_tile_phase_is_aligned_to_the_pad_centroid(self):
        """タイル位相は pad 中心の**重心**（bbox の端や最小値ではない）.

        safe_area bbox 端に位相を合わせると回転で格子が跳び、計画区数が回転角で 暴れる。pad
        重心位相ならタイル位置がタイル数に依存しない。

        pad を x = 0 / 9 / 11 と非対称に置くと重心 20/3 = 6.667mm は bbox の最小値 0・最大値
        11・中点 5.5 のいずれとも違う値になる。safe_area は重心中心の 半径 9mm
        の円なので、位相がそこからずれると（min なら格子は 0 と 10mm、 max なら 1 と 11mm）どのタイルも ROI
        の隅が円外に出て領域 0 個になる。 位相の定義を取り違えた実装をこの 1 件で弁別できる。
        """
        centroid_x = 20.0 / 3.0
        pads = [Point2d(0.0, 2.0), Point2d(9.0, 2.0), Point2d(11.0, 2.0)]

        regions = _plan(
            [_square(centroid_x, 2.0, 3.0)], pads, _solo_area(centroid_x, 2.0)
        )

        assert len(regions) == 1
        assert regions[0].anchor.x == pytest.approx(centroid_x, abs=1e-6)
        assert regions[0].anchor.y == pytest.approx(2.0, abs=1e-6)

    @pytest.mark.parametrize(
        "board_transform", [Shift(5.0, -1.0), Rotation(30.0), Rotation(-45.0)]
    )
    def test_anchor_is_the_board_point_mapped_to_machine_coordinates(
        self, board_transform: Transform
    ):
        """Anchor = board_transform.apply(区中心の board 点)（回転と並進の両方）."""
        regions = _plan(
            [_square(4.0, 2.0, 3.0)],
            [Point2d(4.0, 2.0)],
            _solo_area(4.0, 2.0),
            board_transform=board_transform,
        )

        assert len(regions) == 1
        want = board_transform.apply(Point2d(4.0, 2.0))
        assert regions[0].anchor.x == pytest.approx(want.x, abs=1e-6)
        assert regions[0].anchor.y == pytest.approx(want.y, abs=1e-6)
        # board_center は同じ区中心の board 座標側の表現
        assert regions[0].board_center.x == pytest.approx(4.0, abs=1e-6)
        assert regions[0].board_center.y == pytest.approx(2.0, abs=1e-6)

    def test_roi_is_the_same_image_centered_square_for_every_region(self):
        """全 region の roi が画像中心の region_size_px 正方形で同一.

        アンカーへ移動すると対象領域が画像中心へ来るので、ROI は固定でよい。
        """
        regions = _plan(self.COPPER, self.PADS, self.AREA)

        assert {r.roi for r in regions} == {centered_roi(IMAGE_SIZE, REGION_PX)}

    def test_tile_count_is_stable_under_board_rotation(self):
        """回転を入れても区数が崩れない（±1 個以内）.

        pad 重心位相の直接の効能。実測は θ=0° で 6 区、θ=2° で 5 区。
        """
        upright = len(_plan(self.COPPER, self.PADS, self.AREA))

        rotated = len(
            _plan(
                self.COPPER,
                self.PADS,
                self.AREA,
                board_transform=Rotation(2.0),
            )
        )

        assert abs(rotated - upright) <= 1

    @pytest.mark.parametrize(
        ("tour_start", "first_x"),
        [(Point2d(-40.0, 0.0), -11.0), (Point2d(40.0, 0.0), 9.0)],
    )
    def test_regions_are_ordered_from_the_tour_start(
        self, tour_start: Point2d, first_x: float
    ):
        """巡回起点に近い領域が先頭に来る（index は巡回順に 0 から振り直す）.

        pad 重心 (−1, 0) に位相が合うのでタイル中心は −11 と 9 になる。
        """
        pads = [Point2d(-11.0, 0.0), Point2d(9.0, 0.0)]
        polygons = [_square(p.x, p.y, 3.0) for p in pads]

        regions = _plan(polygons, pads, WIDE_AREA, tour_start=tour_start)

        assert len(regions) == 2
        assert [r.index for r in regions] == [0, 1]
        assert regions[0].anchor.x == pytest.approx(first_x, abs=1e-6)


class TestBoardCenter:
    """board_center: 等サイズ正方格子だから「最近傍の区中心」=「その点を含む区」.

    ``BoardAlignment.correction_for`` はこの性質に全面的に依存している。区中心が
    等間隔の格子に乗っていれば、pad が属する区は「board 座標で最も近い区中心」で
    一意に決まり、包含判定と「区が無い / 失敗したときの近傍探索」を距離最小の
    1 規則で兼ねられる。格子性が崩れる（例: board_center を区中心ではなく区内の
    pad 中心に置く）と、pad が自分の区ではない補正を受け取り得る。

    pad は格子間隔の整数倍から意図的にずらして置く（重心位相なので区中心は pad
    位置と一致しない）。
    """

    # 不規則な x 配置。重心 (1.6, 0.0) が位相になるので区中心は
    # (1.6 + 10i, 10j) に来て、どの pad 中心とも一致しない
    PADS = _grid_pads((-24.0, -13.0, -2.0, 9.0, 38.0), (-8.0, 8.0))
    COPPER = [_square(p.x, p.y, 3.0) for p in PADS]
    AREA = shapely.box(-40.0, -20.0, 50.0, 20.0)

    def _regions(self, board_transform: Transform | None = None):
        regions = _plan(
            self.COPPER, self.PADS, self.AREA, board_transform=board_transform
        )
        assert len(regions) >= 8, "格子性を見るには複数行・複数列の区が必要"
        return regions

    def test_board_centers_lie_on_a_uniform_grid(self):
        """どの 2 区の中心も、軸ごとの差が region_mm の整数倍になる.

        これが「最近傍 = 包含」の前提。board_center を区中心以外（pad 中心・ ROI
        の隅など）にすると整数倍から外れ、最近傍が包含と一致しなくなる。
        """
        regions = self._regions()

        for region in regions:
            for other in regions:
                for delta in (
                    region.board_center.x - other.board_center.x,
                    region.board_center.y - other.board_center.y,
                ):
                    quotient = delta / REGION_MM
                    assert quotient == pytest.approx(round(quotient), abs=1e-6)

    def test_board_center_is_the_anchor_pulled_back_to_board_coordinates(self):
        """board_transform.apply(board_center) == anchor（同じ区の 2 つの表現）."""
        for board_transform in (Identity(), Shift(12.0, -5.0), Rotation(15.0)):
            for region in self._regions(board_transform):
                want = board_transform.apply(region.board_center)
                assert region.anchor.x == pytest.approx(want.x, abs=1e-6)
                assert region.anchor.y == pytest.approx(want.y, abs=1e-6)

    @pytest.mark.parametrize("board_transform", [Identity(), Rotation(25.0)])
    def test_nearest_board_center_is_the_tile_that_contains_the_point(
        self, board_transform: Transform
    ):
        """区内のどの点でも、最近傍の区中心はその区自身になる.

        各区の ROI 内部を 5x5 に刻んで、`min(regions, key=距離)` が必ず自分の区を
        返すことを確かめる。回転が入っても board 座標の距離は pixel 距離の定数倍 （board_transform
        は相似写像）なので同じ性質が成り立つ。
        """
        regions = self._regions(board_transform)
        fractions = (-0.49, -0.25, 0.0, 0.25, 0.49)

        for region in regions:
            for fx in fractions:
                for fy in fractions:
                    # 区中心からの相対位置は board 座標でも軸平行にならないので、
                    # 機械座標で刻んでから board 座標へ引き戻す
                    offset = Point2d(fx * REGION_MM, fy * REGION_MM)
                    point = board_transform.inverse().apply(region.anchor + offset)

                    assert _nearest_region(regions, point).index == region.index, (
                        region.index,
                        fx,
                        fy,
                    )


class TestBoardCenterUnderSkew:
    """スキューを持つ board 変換でも「最近傍 = 包含」が実用範囲で成り立つこと.

    3 点法の board 変換は一般 2x2 なのでスキューを持ち得る（`board.py` の
    `M @ B⁻¹`）。タイルは pixel 空間の正方格子なので board 空間では平行四辺形に
    なり、board 座標で測った Voronoi 分割とは厳密には一致しない。ずれるのは区境界の
    細い帯だけで、実測ではスキュー 0.06° で境界から 1.4um、1° でも 27.9um。
    照合ノイズ（区あたり 5um 級）以下なので実用上は問題にならない。

    「厳密に一致する」ではなく「境界から十分内側なら一致する」を契約にする。
    """

    PADS = TestBoardCenter.PADS
    COPPER = TestBoardCenter.COPPER
    AREA = TestBoardCenter.AREA
    # スキュー 1 度（実測で最悪 27.9um の帯。0.3mm の余裕から見て 10 倍以上小さい）
    SKEW = Matrix2d(np.array([[1.0, math.tan(math.radians(1.0))], [0.0, 1.0]]))

    def test_points_well_inside_a_tile_resolve_to_their_own_tile(self):
        """境界から 0.3mm 以上内側の点は、スキューがあっても自分の区を引く.

        誤った区を引く帯は境界から 30um 以下なので、0.3mm の余裕があれば 1 点も外れない。この余裕を 0 にすると（=
        区の隅ちょうど）保証は無い。
        """
        regions = _plan(self.COPPER, self.PADS, self.AREA, board_transform=self.SKEW)
        assert len(regions) >= 8

        # 半辺 5mm に対して ±4.7mm = 境界から 0.3mm 内側まで
        fractions = (-0.47, -0.25, 0.0, 0.25, 0.47)
        for region in regions:
            for fx in fractions:
                for fy in fractions:
                    offset = Point2d(fx * REGION_MM, fy * REGION_MM)
                    point = self.SKEW.inverse().apply(region.anchor + offset)

                    assert _nearest_region(regions, point).index == region.index, (
                        region.index,
                        fx,
                        fy,
                    )


class TestPadCoverage:
    """塗布対象 pad を含まない区はスキップする."""

    # 3 枚のタイル位置すべてに等方な銅箔があるが、pad は端の 2 つだけに置く
    PADS = [Point2d(-11.0, 0.0), Point2d(9.0, 0.0)]
    COPPER = [_square(x, 0.0, 3.0) for x in (-11.0, -1.0, 9.0)]

    def test_tiles_without_a_pad_center_are_skipped(self):
        """銅箔が広く分布していても、pad 中心が無い区は返らない.

        照合の目的は塗布する pad を合わせること。pad の無い区は測っても
        レバー腕を伸ばす以外の意味がなく、区数が実機時間に直結する。
        """
        regions = _plan(self.COPPER, self.PADS, WIDE_AREA)

        assert len(regions) == 2
        assert sorted(round(r.anchor.x, 6) for r in regions) == [-11.0, 9.0]

    def test_single_pad_yields_only_the_tile_containing_it(self):
        """Pad 1 個だけ渡すと、その pad を含む区だけが返る."""
        regions = _plan(self.COPPER, [Point2d(9.0, 0.0)], WIDE_AREA)

        assert len(regions) == 1
        assert regions[0].anchor.x == pytest.approx(9.0, abs=1e-6)

    def test_no_pad_centers_yields_empty_list(self):
        """塗布対象 pad が空なら領域 0 個（例外は投げない）."""
        assert _plan(self.COPPER, [], WIDE_AREA) == []

    def test_no_copper_yields_empty_list(self):
        """銅箔が無ければ空リスト（不足でも例外は投げない）."""
        assert _plan([], self.PADS, WIDE_AREA) == []


class TestSharpnessThreshold:
    """予測 sharpness による移動前の棄却（λ_min の閾値フィルタ）."""

    STRIP = [_horizontal_strip(-30.0, 30.0)]
    PADS = [Point2d(0.0, 0.0)]

    def test_one_directional_copper_is_rejected_at_the_default_threshold(self):
        """水平エッジしか無い銅箔は λ_min = 0 なので min_sharpness=0.15 で落ちる.

        開口問題の区をステージを動かす前に落とす（実機時間と誤マッチの節約）。
        """
        assert _plan(self.STRIP, self.PADS, WIDE_AREA, min_sharpness=0.15) == []

    def test_one_directional_copper_passes_when_the_threshold_is_zero(self):
        """棄却が閾値によることのピン: min_sharpness=0.0 なら同じ区が返る."""
        regions = _plan(self.STRIP, self.PADS, WIDE_AREA, min_sharpness=0.0)

        assert len(regions) == 1
        assert regions[0].predicted_sharpness == pytest.approx(0.0, abs=1e-9)

    def test_isotropic_copper_passes_the_default_threshold(self):
        """両方向に拘束のある正方リングは予測 sharpness ≈ 0.707 で通る."""
        regions = _plan(
            [_square(0.0, 0.0, 3.0)],
            self.PADS,
            _solo_area(0.0, 0.0),
            min_sharpness=0.15,
        )

        assert len(regions) == 1
        assert regions[0].constraint > 0.0
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
    # 外形いっぱいに銅箔と pad を敷く。外周寄りの区が「pad が無いから」ではなく
    # 「ROI が外周に掛かるから」落ちることを見るための地形。pad 重心が
    # (25.5, 15.5) になるよう並べているので、タイル格子（10mm 間隔）の端の 1 枚は
    # 外形線から 0.5mm の位置に来る = margin 0 なら外周ぎりぎりの区が実際に選ばれる。
    PADS = _grid_pads((5.5, 15.5, 25.5, 35.5, 45.5), (5.5, 15.5, 25.5))
    COPPER = [_square(p.x, p.y, 1.5) for p in PADS]

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

        regions = _plan(
            self.COPPER, self.PADS, safe_area, board_transform=board_transform
        )

        assert (
            regions
        ), "銅箔と pad を敷き詰めた基板なので領域は選ばれるはず（空振り防止）"
        for region in regions:
            roi = _roi_in_board(region, board_transform)
            assert roi.within(safe_area.buffer(1e-9)), region.anchor

    def test_margin_keeps_the_roi_away_from_the_board_edge(self):
        """Margin を 0 → 3mm にすると ROI が外形線から 3mm 以上離れる.

        margin = 0 では外形に接する領域が実際に選ばれる（＝マージンが効いている
        ことの対偶）。この距離差がユーザー要求そのもの。
        """
        no_margin = _plan(self.COPPER, self.PADS, self.OUTLINE)
        with_margin = _plan(self.COPPER, self.PADS, self.OUTLINE.buffer(-3.0))

        def closest_edge_distance(regions: list[AlignmentRegion]) -> float:
            return min(
                self.OUTLINE.exterior.distance(_roi_in_board(r, Identity()))
                for r in regions
            )

        assert closest_edge_distance(no_margin) < 1.0
        assert closest_edge_distance(with_margin) >= 3.0
        assert len(with_margin) < len(no_margin)

    def test_margin_larger_than_the_board_yields_no_regions(self):
        """縮めた外形が空（マージンが基板より大きい）なら領域 0 個・例外なし.

        中止するかどうかは min_regions を持つ呼び出し側の責務。
        """
        safe_area = self.OUTLINE.buffer(-25.0)

        assert safe_area.is_empty
        assert _plan(self.COPPER, self.PADS, safe_area) == []

    def test_safe_area_smaller_than_the_roi_yields_no_regions(self):
        """ROI が収まるタイルが 1 つも無ければ空リスト（例外は投げない）."""
        assert (
            _plan([_square(0.0, 0.0, 3.0)], [Point2d(0.0, 0.0)], _square(0.0, 0.0, 3.0))
            == []
        )


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
            [Point2d(0.0, 0.0)],
            safe_area=_solo_area(0.0, 0.0, region_size_px / PPM),
            region_size_px=region_size_px,
            min_sharpness=0.0,
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
            _plan(
                [_square(0.0, 0.0, 3.0)],
                [Point2d(0.0, 0.0)],
                WIDE_AREA,
                region_size_px=0,
            )

    def test_region_larger_than_the_frame_raises(self):
        with pytest.raises(ValueError, match="region_size_px"):
            _plan(
                [_square(0.0, 0.0, 3.0)],
                [Point2d(0.0, 0.0)],
                WIDE_AREA,
                region_size_px=min(IMAGE_SIZE) + 1,
            )
