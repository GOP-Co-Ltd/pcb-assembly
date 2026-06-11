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

from pcbasm.geometry import Compose, Matrix2d, Point2d, Rotation, Shift
from pcbasm.posctrl import (
    CopperEdgeMatcher,
    CopperProjection,
    CopperProjector,
    EdgeMatch,
)

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
    board_transform: Shift | Compose | None = None,
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
