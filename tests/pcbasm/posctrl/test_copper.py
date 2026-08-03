"""Copper-alignment 仕様テスト.

計画書 memory/agents/implementation-planner/copper-alignment.md §1/§2/§4 に基づく。
投影公式: pixel(b, s) = image_center_px + ppm * offset_transform.apply(
s - board_transform.apply(b))。x 右・y 下、image_center は全画面中心。
EdgeMatch.offset は「観測 - 想定」(px)。
"""

import cv2
import numpy as np
import pytest
import shapely

from pcbasm.geometry import Compose, Matrix2d, Point2d, Rotation, Shift, Transform
from pcbasm.posctrl import (
    CopperEdgeMatcher,
    CopperProjection,
    CopperProjector,
    EdgeMatch,
)
from pcbasm.vision import Offset

PPM = 10.0  # pixel/mm
MATCH_ROI = (20, 20, 180, 180)


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


def _projector(
    polygons: list[shapely.Polygon],
    board_transform: Transform | None = None,
    offset_transform: Rotation | Compose | None = None,
    image_size: tuple[int, int] = (200, 200),
) -> CopperProjector:
    """恒等変換をデフォルトとする CopperProjector を組み立てる."""
    if board_transform is None:
        board_transform = Shift(0.0, 0.0)
    if offset_transform is None:
        offset_transform = Rotation(0.0)
    return CopperProjector(
        polygons=polygons,
        board_transform=board_transform,
        offset_transform=offset_transform,
        pixel_per_mm=PPM,
        image_size=image_size,
    )


def _centroid(mask: np.ndarray) -> np.ndarray:
    """非ゼロ画素の重心 (row, col) を返す."""
    return np.argwhere(mask > 0).mean(axis=0)


def _edge_ring(shift_x: int = 0, shift_y: int = 0) -> np.ndarray:
    """(60,60)-(140,140) の矩形リングを指定 px ずらした 200x200 エッジマスク."""
    mask = np.zeros((200, 200), dtype=np.uint8)
    cv2.rectangle(
        mask,
        (60 + shift_x, 60 + shift_y),
        (140 + shift_x, 140 + shift_y),
        255,
        1,
    )
    return mask


def _subpixel_circles(shift_x: float = 0.0, shift_y: float = 0.0) -> np.ndarray:
    """1/16px 精度で描いた円4個を非整数量ずらしたエッジマスク."""
    mask = np.zeros((200, 200), dtype=np.uint8)
    for offset_x, offset_y in ((-30, -30), (30, -30), (-30, 30), (30, 30)):
        cv2.circle(
            mask,
            (
                round((100 + offset_x + shift_x) * 16),
                round((100 + offset_y + shift_y) * 16),
            ),
            15 * 16,
            255,
            1,
            shift=4,
        )
    return mask


def _horizontal_edge(y: int) -> np.ndarray:
    """X方向に拘束のない水平エッジ."""
    mask = np.zeros((200, 200), dtype=np.uint8)
    cv2.line(mask, (10, y), (190, y), 255, 1)
    return mask


def _rect_vertices(center: Point2d, half_w: float, half_h: float) -> list[Point2d]:
    """中心 center・半幅 half_w/half_h の矩形頂点列 (px)."""
    return [
        Point2d(center.x - half_w, center.y - half_h),
        Point2d(center.x + half_w, center.y - half_h),
        Point2d(center.x + half_w, center.y + half_h),
        Point2d(center.x - half_w, center.y + half_h),
    ]


def _draw_ring(
    vertices: list[Point2d], shape: tuple[int, int] = (200, 200)
) -> np.ndarray:
    """頂点列を 1px 幅の閉ポリラインとして描画したエッジマスクを返す."""
    mask = np.zeros(shape, dtype=np.uint8)
    points = np.round(np.array([[v.x, v.y] for v in vertices]))
    points = points.astype(np.int32).reshape(-1, 1, 2)
    cv2.polylines(mask, [points], isClosed=True, color=255, thickness=1)
    return mask


class TestCopperProjector:
    """CopperProjector の投影公式・マスク生成のテスト."""

    def test_identity_transform_projects_square_at_expected_pixels(self):
        """恒等変換 + ppm=10 で既知正方形が期待 pixel 位置に投影される.

        pixel = center + ppm * (s - b)。s=(0,0) なので b=±2mm → center ∓ 20px。
        image_size=(width, height)=(320, 200) → マスク shape は (200, 320)、
        image_center は (160, 100)。正方形は cols 140..180, rows 80..120。
        """
        projection = _projector(
            [_square(0.0, 0.0, 2.0)], image_size=(320, 200)
        ).project(Point2d(0.0, 0.0))

        assert isinstance(projection, CopperProjection)
        for mask in (projection.fill_mask, projection.edge_mask):
            assert mask.shape == (200, 320)
            assert mask.dtype == np.uint8
            assert set(np.unique(mask)) <= {0, 255}
        fill = projection.fill_mask
        assert np.all(fill[85:116, 145:176] == 255)  # 内部は塗り潰し
        assert not np.any(fill[:75, :])  # 正方形の外側 4 方向
        assert not np.any(fill[126:, :])
        assert not np.any(fill[:, :135])
        assert not np.any(fill[:, 186:])
        edge = projection.edge_mask
        assert np.any(edge[78:83, 150:171] > 0)  # 上辺 (row≈80)
        assert np.any(edge[118:123, 150:171] > 0)  # 下辺 (row≈120)
        assert np.any(edge[90:111, 138:143] > 0)  # 左辺 (col≈140)
        assert np.any(edge[90:111, 178:183] > 0)  # 右辺 (col≈180)
        assert not np.any(edge[85:116, 145:176])  # 内部にエッジなし

    def test_stage_plus_x_shifts_projection_plus_x_pixels(self):
        """符号ピン: stage_xy +1mm(x) で投影が +10px(x) 動く（公式の sign 固定）."""
        projector = _projector([_square(0.0, 0.0, 2.0)])

        at_origin = projector.project(Point2d(0.0, 0.0))
        at_plus_x = projector.project(Point2d(1.0, 0.0))

        c0 = _centroid(at_origin.fill_mask)
        c1 = _centroid(at_plus_x.fill_mask)
        assert c1[1] - c0[1] == pytest.approx(PPM, abs=1.0)  # col(x): +10px
        assert c1[0] - c0[0] == pytest.approx(0.0, abs=1.0)  # row(y): 不変

    def test_rotation_180_offset_transform_projects_point_symmetric(self):
        """offset_transform=Rotation(180) で投影が画像中心の点対称になる."""
        polygons = [_square(2.0, 1.0, 0.5)]
        plain = _projector(polygons, offset_transform=Rotation(0.0))
        rotated = _projector(polygons, offset_transform=Rotation(180.0))

        c_plain = _centroid(plain.project(Point2d(0.0, 0.0)).fill_mask)
        c_rot = _centroid(rotated.project(Point2d(0.0, 0.0)).fill_mask)

        # Rotation(0): pixel = (100,100) - 10*(2,1) = (80, 90) → (row 90, col 80)
        assert c_plain[0] == pytest.approx(90.0, abs=1.0)
        assert c_plain[1] == pytest.approx(80.0, abs=1.0)
        # Rotation(180): 画像中心 (100,100) の点対称 → c_rot ≈ 200 - c_plain
        assert c_rot[0] == pytest.approx(200.0 - c_plain[0], abs=1.0)
        assert c_rot[1] == pytest.approx(200.0 - c_plain[1], abs=1.0)

    def test_mirror_matrix_and_shift_compose_is_reflected_in_projection(self):
        """反転 Matrix2d + Shift の Compose を board_transform に与えると正しく反映.

        T_b(b) = mirror_x(b) + (2, 1)。b0=(1, 0.5) → T_b(b0)=(1, 1.5)、
        pixel = (100,100) + 10*((0,0) - (1, 1.5)) = (90, 85)。
        """
        board_transform = Compose([Matrix2d(np.diag([-1.0, 1.0])), Shift(2.0, 1.0)])
        projector = _projector(
            [_square(1.0, 0.5, 0.5)], board_transform=board_transform
        )

        projection = projector.project(Point2d(0.0, 0.0))

        centroid = _centroid(projection.fill_mask)
        assert centroid[1] == pytest.approx(90.0, abs=1.0)  # col(x)
        assert centroid[0] == pytest.approx(85.0, abs=1.0)  # row(y)
        assert projection.fill_mask[85, 90] == 255

    def test_polygon_hole_is_unfilled_and_inner_ring_edged(self):
        """穴付き polygon は fill の穴が 0、edge に内外 2 リングが描かれる."""
        polygon = shapely.Polygon(
            [(-3.0, -3.0), (3.0, -3.0), (3.0, 3.0), (-3.0, 3.0)],
            holes=[[(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]],
        )

        projection = _projector([polygon]).project(Point2d(0.0, 0.0))

        # 外周 cols/rows 70..130、穴 90..110
        fill = projection.fill_mask
        assert fill[100, 100] == 0  # 穴の中心
        assert fill[100, 80] == 255  # 外周と穴の間のバンド
        assert fill[100, 65] == 0  # polygon の外
        edge = projection.edge_mask
        assert np.any(edge[98:103, 68:73] > 0)  # 外リング左辺 (col≈70)
        assert np.any(edge[98:103, 88:93] > 0)  # 内リング左辺 (col≈90)
        assert not np.any(edge[98:103, 78:84])  # バンド中央にエッジなし

    def test_out_of_view_polygon_yields_empty_masks(self):
        """視野外の polygon は何も描かれない（bbox フィルタ経路）."""
        projection = _projector([_square(100.0, 100.0, 1.0)]).project(Point2d(0.0, 0.0))

        assert not np.any(projection.fill_mask)
        assert not np.any(projection.edge_mask)

    def test_clipped_polygon_has_no_false_edge_on_frame_border(self):
        """フレームからはみ出す polygon でも縁にクリップ偽エッジが出ない.

        x∈[-15, 5]mm → pixel cols 50..250（右へはみ出す）。fill は右端まで
        届くが、edge の最終列には水平 2 辺の通過分しか現れない（fill の輪郭
        からエッジを作ると右端に縦の偽エッジ線が出る: polylines 方式のピン）。
        """
        polygon = shapely.Polygon(
            [(-15.0, -3.0), (5.0, -3.0), (5.0, 3.0), (-15.0, 3.0)]
        )

        projection = _projector([polygon]).project(Point2d(0.0, 0.0))

        assert projection.fill_mask[100, 199] == 255  # fill は右端まで届く
        assert np.any(projection.edge_mask[80:120, 49:52] > 0)  # 見えている辺
        # 最終列は水平 2 辺 (rows≈70,130) の通過分のみ。縦の偽エッジ線はない
        assert np.count_nonzero(projection.edge_mask[:, 199]) <= 4

    def test_pixel_of_matches_projection_formula(self):
        """pixel_of が投影公式 center + ppm * R(s − T_b(b)) と一致する.

        計画書 pad-alignment.md: _pixel_of の公開化（符号ピンの公開面への移譲）。
        T_b=Shift(2,1), R=Rotation(90), image_size=(320,200) →
        center=(160,100)。 b=(1,0.5), s=(1,2): s−T_b(b)=(−2,0.5) → R90 →
        (−0.5,−2) → pixel=(160−5, 100−20)=(155, 80)。
        """
        projector = _projector(
            [_square(0.0, 0.0, 2.0)],
            board_transform=Shift(2.0, 1.0),
            offset_transform=Rotation(90.0),
            image_size=(320, 200),
        )

        pixel = projector.pixel_of(Point2d(1.0, 0.5), Point2d(1.0, 2.0))

        assert pixel.x == pytest.approx(155.0, abs=1e-6)
        assert pixel.y == pytest.approx(80.0, abs=1e-6)


class TestCopperEdgeMatcher:
    """CopperEdgeMatcher の chamfer マッチングのテスト."""

    @pytest.fixture
    def matcher(self) -> CopperEdgeMatcher:
        """標準 matcher (10 px/mm, 窓 2mm = 20px, crop なし)."""
        return CopperEdgeMatcher(pixel_per_mm=PPM)

    def test_match_recovers_known_pixel_shift(self, matcher: CopperEdgeMatcher):
        """観測が想定から (+7, -4)px ずれた矩形リング → offset.px ≈ (7, -4).

        符号ピン: offset は「観測 - 想定」で、想定エッジを offset だけ
        動かすと観測に重なる向き。
        """
        observed = _edge_ring(7, -4)
        expected = _edge_ring()

        match = matcher.match(observed, expected, MATCH_ROI)

        assert match is not None
        assert match.offset.px.x == pytest.approx(7.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(-4.0, abs=1.0)

    def test_identical_masks_match_with_zero_offset(self, matcher: CopperEdgeMatcher):
        """観測と想定が完全一致 → offset (0,0) かつ mean_distance ≈ 0."""
        match = matcher.match(_edge_ring(), _edge_ring(), MATCH_ROI)

        assert isinstance(match, EdgeMatch)
        assert match.offset.px.x == pytest.approx(0.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(0.0, abs=1.0)
        assert match.rms_distance_px == pytest.approx(0.0, abs=0.5)

    @pytest.mark.parametrize(
        ("shift_x", "shift_y"),
        [(2.4, -1.2), (-1.6, 0.8), (0.5, 0.5)],
    )
    def test_match_recovers_subpixel_shift(
        self, matcher: CopperEdgeMatcher, shift_x: float, shift_y: float
    ):
        match = matcher.match(
            _subpixel_circles(shift_x, shift_y),
            _subpixel_circles(),
            MATCH_ROI,
        )

        assert match is not None
        assert match.offset.px.x == pytest.approx(shift_x, abs=0.15)
        assert match.offset.px.y == pytest.approx(shift_y, abs=0.15)

    def test_match_recovers_shift_despite_noise_edges(self, matcher: CopperEdgeMatcher):
        """観測にノイズエッジ画素が混ざってもずれを復元できる."""
        observed = _edge_ring(6, -2)
        rng = np.random.default_rng(seed=42)
        noise = rng.integers(0, 200, size=(40, 2))
        observed[noise[:, 0], noise[:, 1]] = 255

        match = matcher.match(observed, _edge_ring(), MATCH_ROI)

        assert match is not None
        assert match.offset.px.x == pytest.approx(6.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(-2.0, abs=1.0)

    def test_empty_observed_or_expected_mask_returns_none(
        self, matcher: CopperEdgeMatcher
    ):
        """どちらかのマスクが空なら None を返す."""
        empty = np.zeros((200, 200), dtype=np.uint8)

        assert matcher.match(empty, _edge_ring(), MATCH_ROI) is None
        assert matcher.match(_edge_ring(), empty, MATCH_ROI) is None

    def test_shift_beyond_window_returns_none(self, matcher: CopperEdgeMatcher):
        """窓 (2mm=20px) を超えるずれは探索窓端の偽解として棄却する."""
        match = matcher.match(_edge_ring(30, 0), _edge_ring(), MATCH_ROI)

        assert match is None

    def test_one_directional_edges_return_none(self, matcher: CopperEdgeMatcher):
        """一方向にしか拘束のない領域は解を捏造せず棄却する."""
        assert (
            matcher.match(_horizontal_edge(102), _horizontal_edge(100), MATCH_ROI)
            is None
        )

    def test_expected_edges_outside_crop_do_not_affect_match(self):
        """crop_size 外の想定エッジ（別のずれを示唆する構造）が結果に影響しない."""
        matcher = CopperEdgeMatcher(pixel_per_mm=PPM, search_window_mm=1.0)
        expected = np.zeros((200, 200), dtype=np.uint8)
        observed = np.zeros((200, 200), dtype=np.uint8)
        # crop 内 (rows/cols 50..150): (+4, +2)px ずれたリング
        cv2.rectangle(expected, (70, 70), (130, 130), 255, 1)
        cv2.rectangle(observed, (74, 72), (134, 132), 255, 1)
        # crop + 窓 (10px) の外: 逆向き (-8, -6) のずれを示唆する構造
        cv2.rectangle(expected, (5, 5), (38, 38), 255, 1)
        cv2.rectangle(expected, (12, 12), (31, 31), 255, 1)
        cv2.rectangle(observed, (5 - 8, 5 - 6), (38 - 8, 38 - 6), 255, 1)
        cv2.rectangle(observed, (12 - 8, 12 - 6), (31 - 8, 31 - 6), 255, 1)

        match = matcher.match(observed, expected, (50, 50, 150, 150))

        assert match is not None
        assert match.offset.px.x == pytest.approx(4.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(2.0, abs=1.0)


class TestCopperEdgeMatcherRoi:
    """CopperEdgeMatcher.match の ROI 限定照合のテスト.

    θ 撤去（memory/agents/spec-test-author/pad-align-drop-theta.md）により
    match_rigid は match に統合され、照合は並進のみになった。ROI はテンプレートとして切り出す矩形（全画面
    px）で、ROI 外の構造は結果に影響してはならない。
    """

    @pytest.fixture
    def matcher(self) -> CopperEdgeMatcher:
        """標準 matcher (10 px/mm, 窓 2mm = 20px)."""
        return CopperEdgeMatcher(pixel_per_mm=PPM)

    def test_match_with_roi_recovers_known_translation(
        self, matcher: CopperEdgeMatcher
    ):
        """ROI 指定で既知の並進 (+5,+3)px を復元する.

        ROI 中心 (240,180) は画像中心 (200,200) と異なる。並進のみの照合なので 結果は ROI
        中心の取り方に依存しない（回転中心という概念がない）。
        """
        center = Point2d(240.0, 180.0)
        vertices = _rect_vertices(center, 120.0, 90.0)
        moved = [v + Point2d(5.0, 3.0) for v in vertices]
        expected = _draw_ring(vertices, shape=(400, 400))
        observed = _draw_ring(moved, shape=(400, 400))

        match = matcher.match(observed, expected, roi=(110, 80, 370, 280))

        assert match is not None
        assert match.offset.px.x == pytest.approx(5.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(3.0, abs=1.0)

    def test_camera_transform_maps_expected_vertices_onto_observed(
        self, matcher: CopperEdgeMatcher
    ):
        """camera_transform（想定→観測）が合成時の頂点対応を mm 空間で再現する."""
        center = Point2d(240.0, 180.0)
        image_center = Point2d(200.0, 200.0)
        vertices = _rect_vertices(center, 120.0, 90.0)
        moved = [v + Point2d(5.0, 3.0) for v in vertices]
        expected = _draw_ring(vertices, shape=(400, 400))
        observed = _draw_ring(moved, shape=(400, 400))

        match = matcher.match(observed, expected, roi=(110, 80, 370, 280))

        assert match is not None
        transform = match.camera_transform
        for v_expected, v_observed in zip(vertices, moved, strict=True):
            mapped = transform.apply((v_expected - image_center) / PPM)
            target = (v_observed - image_center) / PPM
            assert mapped.x == pytest.approx(target.x, abs=0.2)
            assert mapped.y == pytest.approx(target.y, abs=0.2)

    def test_match_uses_only_template_inside_roi(self):
        """ROI 外の逆ずれ構造が照合結果に影響しない（pad ROI 限定の核心）."""
        matcher = CopperEdgeMatcher(pixel_per_mm=PPM, search_window_mm=1.0)
        expected = np.zeros((200, 200), dtype=np.uint8)
        observed = np.zeros((200, 200), dtype=np.uint8)
        # ROI 内 (50..150): (+4, +2)px ずれたリング
        cv2.rectangle(expected, (70, 70), (130, 130), 255, 1)
        cv2.rectangle(observed, (74, 72), (134, 132), 255, 1)
        # ROI + 窓 (10px) の外: 逆向き (-8, -6) のずれを示唆する構造
        cv2.rectangle(expected, (5, 5), (38, 38), 255, 1)
        cv2.rectangle(expected, (12, 12), (31, 31), 255, 1)
        cv2.rectangle(observed, (5 - 8, 5 - 6), (38 - 8, 38 - 6), 255, 1)
        cv2.rectangle(observed, (12 - 8, 12 - 6), (31 - 8, 31 - 6), 255, 1)

        match = matcher.match(observed, expected, roi=(50, 50, 150, 150))

        assert match is not None
        assert match.offset.px.x == pytest.approx(4.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(2.0, abs=1.0)

    def test_match_is_stable_with_edges_crossing_roi_boundary(
        self, matcher: CopperEdgeMatcher
    ):
        """ROI 境界を横断する長いエッジがあっても既知ずれを復元する.

        エッジ線マスクの矩形切出しは新規画素を生まない（クリップ偽エッジ ゼロ）ことのピン。フレーム全幅の水平線が ROI を横断する。
        """
        expected = np.zeros((200, 200), dtype=np.uint8)
        observed = np.zeros((200, 200), dtype=np.uint8)
        cv2.rectangle(expected, (70, 70), (130, 130), 255, 1)
        cv2.line(expected, (0, 100), (199, 100), 255, 1)
        # 全体を (+3, −2)px ずらした観測
        cv2.rectangle(observed, (73, 68), (133, 128), 255, 1)
        cv2.line(observed, (0, 98), (199, 98), 255, 1)

        match = matcher.match(observed, expected, roi=(50, 50, 150, 150))

        assert match is not None
        assert match.offset.px.x == pytest.approx(3.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(-2.0, abs=1.0)

    def test_match_returns_none_when_roi_has_no_expected_edges(
        self, matcher: CopperEdgeMatcher
    ):
        """ROI 内に想定エッジが無い場合は None（偽照合しない）."""
        expected = np.zeros((200, 200), dtype=np.uint8)
        cv2.rectangle(expected, (5, 5), (50, 50), 255, 1)

        match = matcher.match(_edge_ring(), expected, roi=(60, 60, 140, 140))

        assert match is None


class TestEdgeMatch:
    """EdgeMatch の camera_transform 契約のテスト."""

    def test_camera_transform_is_pure_shift_by_offset(self):
        """camera_transform = o ↦ o + d（d = offset.mm）.

        θ 撤去後の camera_transform は Shift.from_point(offset.mm) と等価な純並進。
        回転中心という概念が無く、どの点でも同じ変位ベクトルになる。
        """
        match = EdgeMatch(
            offset=Offset(px=Point2d(12.0, -8.0), pixel_per_mm=PPM),
            rms_distance_px=0.0,
        )

        transform = match.camera_transform

        d = Point2d(1.2, -0.8)  # offset.mm
        for point in (Point2d(0.0, 0.0), Point2d(2.0, 1.0), Point2d(-30.0, 40.0)):
            mapped = transform.apply(point)
            assert mapped.x == pytest.approx(point.x + d.x, abs=1e-9)
            assert mapped.y == pytest.approx(point.y + d.y, abs=1e-9)
