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
from pcbasm.posctrl.copper import RigidEdgeMatch
from pcbasm.vision import Offset

PPM = 10.0  # pixel/mm


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


def _rect_vertices(center: Point2d, half_w: float, half_h: float) -> list[Point2d]:
    """中心 center・半幅 half_w/half_h の矩形頂点列 (px)."""
    return [
        Point2d(center.x - half_w, center.y - half_h),
        Point2d(center.x + half_w, center.y - half_h),
        Point2d(center.x + half_w, center.y + half_h),
        Point2d(center.x - half_w, center.y + half_h),
    ]


def _rotated_vertices(
    vertices: list[Point2d], degrees: float, center: Point2d
) -> list[Point2d]:
    """頂点列を center 回りに自前 Rotation 規約で回転する.

    pcbasm.geometry.Rotation の数式（x右・y下の pixel 座標へそのまま適用）を ground truth
    とするため、warpAffine には依存しない。
    """
    rotation = Rotation(degrees)
    return [rotation.apply(v - center) + center for v in vertices]


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

    def test_roi_of_returns_projected_bbox_with_margin(self):
        """roi_of = exterior 全頂点の投影 bbox + マージン (1mm=10px).

        ±2mm 角の投影 bbox は 80..120px、margin ±10px → 70..130（丸め ±1px）。
        """
        projector = _projector([])

        x0, y0, x1, y1 = projector.roi_of(
            _square(0.0, 0.0, 2.0), Point2d(0.0, 0.0), margin_mm=1.0, min_size_mm=3.0
        )

        assert x0 == pytest.approx(70, abs=1)
        assert y0 == pytest.approx(70, abs=1)
        assert x1 == pytest.approx(130, abs=1)
        assert y1 == pytest.approx(130, abs=1)

    def test_roi_of_expands_small_pad_to_min_size(self):
        """0.5mm 角 pad は margin 込み 25px → min_size 3mm=30px へ中心対称拡張."""
        projector = _projector([])

        x0, y0, x1, y1 = projector.roi_of(
            _square(0.0, 0.0, 0.25), Point2d(0.0, 0.0), margin_mm=1.0, min_size_mm=3.0
        )

        assert x1 - x0 >= 30
        assert y1 - y0 >= 30
        assert x1 - x0 <= 33  # 過剰拡張しない
        assert y1 - y0 <= 33
        assert (x0 + x1) / 2 == pytest.approx(100.0, abs=1.5)  # 中心対称
        assert (y0 + y1) / 2 == pytest.approx(100.0, abs=1.5)

    def test_roi_of_clamps_to_frame(self):
        """フレームからはみ出す投影 bbox はフレーム境界へクランプされる."""
        projector = _projector([])

        roi = projector.roi_of(_square(0.0, 0.0, 12.0), Point2d(0.0, 0.0))

        assert roi == (0, 0, 200, 200)

    def test_roi_of_covers_all_vertices_under_rotated_board_transform(self):
        """回転 board_transform では全頂点の投影 bbox を取る（mm bbox の変換ではない）.

        三角形 (−2,0),(2,0),(0,3) を Rotation(45) で回すと投影頂点は
        x: 85.9/114.1/121.2, y: 78.8/85.9/114.1 → bbox+10px ≈ (76, 69, 131, 124)。
        board 空間 bbox の4隅を変換する誤実装では x1 ≈ 145 になり区別できる。
        """
        projector = _projector([], board_transform=Rotation(45.0))
        triangle = shapely.Polygon([(-2.0, 0.0), (2.0, 0.0), (0.0, 3.0)])

        x0, y0, x1, y1 = projector.roi_of(triangle, Point2d(0.0, 0.0))

        assert x0 == pytest.approx(75.9, abs=2)
        assert y0 == pytest.approx(68.8, abs=2)
        assert x1 == pytest.approx(131.2, abs=2)
        assert y1 == pytest.approx(124.1, abs=2)


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

        match = matcher.match(observed, expected)

        assert match is not None
        assert match.offset.px.x == pytest.approx(7.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(-4.0, abs=1.0)

    def test_identical_masks_match_with_zero_offset(self, matcher: CopperEdgeMatcher):
        """観測と想定が完全一致 → offset (0,0) かつ mean_distance ≈ 0."""
        match = matcher.match(_edge_ring(), _edge_ring())

        assert isinstance(match, EdgeMatch)
        assert match.offset.px.x == pytest.approx(0.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(0.0, abs=1.0)
        assert match.mean_distance_px == pytest.approx(0.0, abs=0.5)

    def test_match_recovers_shift_with_partially_missing_observed_edges(
        self, matcher: CopperEdgeMatcher
    ):
        """観測エッジの下半分が欠損していてもずれを復元できる."""
        observed = _edge_ring(5, 3)
        observed[110:, :] = 0  # 下半分欠損（下辺と縦辺の下部が消える）

        match = matcher.match(observed, _edge_ring())

        assert match is not None
        assert match.offset.px.x == pytest.approx(5.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(3.0, abs=1.0)

    def test_match_recovers_shift_despite_noise_edges(self, matcher: CopperEdgeMatcher):
        """観測にノイズエッジ画素が混ざってもずれを復元できる."""
        observed = _edge_ring(6, -2)
        rng = np.random.default_rng(seed=42)
        noise = rng.integers(0, 200, size=(40, 2))
        observed[noise[:, 0], noise[:, 1]] = 255

        match = matcher.match(observed, _edge_ring())

        assert match is not None
        assert match.offset.px.x == pytest.approx(6.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(-2.0, abs=1.0)

    def test_empty_observed_or_expected_mask_returns_none(
        self, matcher: CopperEdgeMatcher
    ):
        """どちらかのマスクが空なら None を返す."""
        empty = np.zeros((200, 200), dtype=np.uint8)

        assert matcher.match(empty, _edge_ring()) is None
        assert matcher.match(_edge_ring(), empty) is None

    def test_shift_beyond_window_stays_in_window_with_large_distance(
        self, matcher: CopperEdgeMatcher
    ):
        """窓 (2mm=20px) を超える 30px ずれ → |offset| ≤ 窓、mean_distance 大."""
        match = matcher.match(_edge_ring(30, 0), _edge_ring())

        assert match is not None
        assert abs(match.offset.px.x) <= 21.0
        assert abs(match.offset.px.y) <= 21.0
        assert match.mean_distance_px > 2.0

    def test_expected_edges_outside_crop_do_not_affect_match(self):
        """crop_size 外の想定エッジ（別のずれを示唆する構造）が結果に影響しない."""
        matcher = CopperEdgeMatcher(
            pixel_per_mm=PPM, search_window_mm=1.0, crop_size=(100, 100)
        )
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

        match = matcher.match(observed, expected)

        assert match is not None
        assert match.offset.px.x == pytest.approx(4.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(2.0, abs=1.0)


class TestCopperEdgeMatcherRigid:
    """CopperEdgeMatcher.match_rigid（並進+微小回転）のテスト.

    計画書 pad-alignment.md「match_rigid」に基づく。rotation は ROI 中心回り、 符号は
    pcbasm.geometry.Rotation の規約（pixel/カメラmm 座標系、x右・y下）。 回転データの合成は想定頂点列を
    Rotation.apply で回してから polylines 描画し、 実装側の warpAffine とは独立な ground
    truth とする。
    """

    @pytest.fixture
    def matcher(self) -> CopperEdgeMatcher:
        """標準 rigid matcher (10 px/mm, 窓2mm, θ±2.0°/coarse0.5°/fine0.1°)."""
        return CopperEdgeMatcher(pixel_per_mm=PPM)

    def test_match_rigid_recovers_pure_translation_with_zero_rotation(
        self, matcher: CopperEdgeMatcher
    ):
        """純並進 (+7,−4)px → offset≈(7,−4)・θ≈0・center_mm≈画像中心 (0,0).

        roi=None は中央 crop へフォールバックし、回転中心はその中心 （=画像中心、カメラ mm 原点）になる。
        """
        match = matcher.match_rigid(_edge_ring(7, -4), _edge_ring())

        assert isinstance(match, RigidEdgeMatch)
        assert match.offset.px.x == pytest.approx(7.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(-4.0, abs=1.0)
        assert match.rotation.degrees == pytest.approx(0.0, abs=0.15)
        assert match.center_mm.x == pytest.approx(0.0, abs=0.1)
        assert match.center_mm.y == pytest.approx(0.0, abs=0.1)

    def test_match_rigid_recovers_positive_rotation_sign(
        self, matcher: CopperEdgeMatcher
    ):
        """符号ピン: 想定リングを Rotation(+1.2°) で回した観測 → θ ≈ +1.2.

        頂点列を自前 Rotation 規約で画像中心回りに回してから描画する。 getRotationMatrix2D
        等の外部規約に依存した実装はここで符号が割れる。
        """
        center = Point2d(200.0, 200.0)
        vertices = _rect_vertices(center, 120.0, 90.0)
        expected = _draw_ring(vertices, shape=(400, 400))
        observed = _draw_ring(
            _rotated_vertices(vertices, 1.2, center), shape=(400, 400)
        )

        match = matcher.match_rigid(observed, expected)

        assert match is not None
        assert match.rotation.degrees == pytest.approx(1.2, abs=0.2)
        assert match.offset.px.x == pytest.approx(0.0, abs=1.5)
        assert match.offset.px.y == pytest.approx(0.0, abs=1.5)

    def test_match_rigid_recovers_translation_and_rotation_together(
        self, matcher: CopperEdgeMatcher
    ):
        """ROI 中心回り +1.0° と並進 (+5,+3)px の合成を同時に復元する.

        ROI 中心 (240,180) は画像中心 (200,200) と異なり、center_mm は カメラ
        mm（画像中心原点）で (4,−2) になる。θ の許容幅は 1px ラスタ化での 分解能（fine 0.1° +
        並進量子化との結合）を考慮した値。
        """
        center = Point2d(240.0, 180.0)
        vertices = _rect_vertices(center, 120.0, 90.0)
        moved = [
            v + Point2d(5.0, 3.0) for v in _rotated_vertices(vertices, 1.0, center)
        ]
        expected = _draw_ring(vertices, shape=(400, 400))
        observed = _draw_ring(moved, shape=(400, 400))

        match = matcher.match_rigid(observed, expected, roi=(110, 80, 370, 280))

        assert match is not None
        assert match.offset.px.x == pytest.approx(5.0, abs=1.5)
        assert match.offset.px.y == pytest.approx(3.0, abs=1.5)
        assert match.rotation.degrees == pytest.approx(1.0, abs=0.3)
        assert match.center_mm.x == pytest.approx(4.0, abs=0.15)
        assert match.center_mm.y == pytest.approx(-2.0, abs=0.15)

    def test_camera_transform_round_trips_expected_to_observed(
        self, matcher: CopperEdgeMatcher
    ):
        """camera_transform（想定→観測）が合成時の頂点対応を mm 空間で再現する.

        観測は ROI 中心回り +1.0° + (5,3)px で合成しているので、各想定頂点を camera_transform
        に通すと対応する観測頂点（カメラ mm）に重なる。
        """
        center = Point2d(240.0, 180.0)
        image_center = Point2d(200.0, 200.0)
        vertices = _rect_vertices(center, 120.0, 90.0)
        moved = [
            v + Point2d(5.0, 3.0) for v in _rotated_vertices(vertices, 1.0, center)
        ]
        expected = _draw_ring(vertices, shape=(400, 400))
        observed = _draw_ring(moved, shape=(400, 400))

        match = matcher.match_rigid(observed, expected, roi=(110, 80, 370, 280))

        assert match is not None
        transform = match.camera_transform
        for v_expected, v_observed in zip(vertices, moved, strict=True):
            mapped = transform.apply((v_expected - image_center) / PPM)
            target = (v_observed - image_center) / PPM
            assert mapped.x == pytest.approx(target.x, abs=0.2)
            assert mapped.y == pytest.approx(target.y, abs=0.2)

    def test_match_rigid_uses_only_template_inside_roi(self):
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

        match = matcher.match_rigid(observed, expected, roi=(50, 50, 150, 150))

        assert match is not None
        assert match.offset.px.x == pytest.approx(4.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(2.0, abs=1.0)
        assert match.rotation.degrees == pytest.approx(0.0, abs=0.2)

    def test_match_rigid_is_stable_with_edges_crossing_roi_boundary(
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

        match = matcher.match_rigid(observed, expected, roi=(50, 50, 150, 150))

        assert match is not None
        assert match.offset.px.x == pytest.approx(3.0, abs=1.0)
        assert match.offset.px.y == pytest.approx(-2.0, abs=1.0)

    def test_match_rigid_returns_none_when_roi_has_no_expected_edges(
        self, matcher: CopperEdgeMatcher
    ):
        """ROI 内に想定エッジが無い場合は None（偽照合しない）."""
        expected = np.zeros((200, 200), dtype=np.uint8)
        cv2.rectangle(expected, (5, 5), (50, 50), 255, 1)

        match = matcher.match_rigid(_edge_ring(), expected, roi=(60, 60, 140, 140))

        assert match is None

    def test_camera_transform_formula_is_rotation_about_center_plus_offset(self):
        """camera_transform = o ↦ Rot_θ(o−c) + c + d（c=center_mm, d=offset.mm）.

        計画書の docstring 式を直接ピン留めする公開 API 契約。
        """
        center = Point2d(2.0, 1.0)
        match = RigidEdgeMatch(
            offset=Offset(px=Point2d(12.0, -8.0), pixel_per_mm=PPM),
            rotation=Rotation(30.0),
            center_mm=center,
            mean_distance_px=0.0,
        )

        transform = match.camera_transform

        d = Point2d(1.2, -0.8)  # offset.mm
        for point in (Point2d(0.0, 0.0), center, Point2d(3.0, 1.0)):
            expected = Rotation(30.0).apply(point - center) + center + d
            mapped = transform.apply(point)
            assert mapped.x == pytest.approx(expected.x, abs=1e-9)
            assert mapped.y == pytest.approx(expected.y, abs=1e-9)
