"""Copper-alignment 仕様テスト.

計画書 memory/agents/implementation-planner/region-alignment-average.md
「公開インターフェース → src/pcbasm/posctrl/copper.py」に基づく。

投影公式: pixel(b, s) = image_center_px + ppm * offset_transform.apply(
s - board_transform.apply(b))。x 右・y 下、image_center は全画面中心。
EdgeMatch.offset は「観測 - 想定」(px、サブピクセル)。

照合の核心は「拘束不足（開口問題）の領域を自ら棄却する」こと。一方向の
エッジしか無い ROI ではコスト曲面が平坦になり、旧実装は
mean_distance_px ≈ 0 のまま弱軸方向のデタラメな並進を返していた。
sharpness（弱軸方向へ 1px ずらしたときの RMS 距離の増分）が
min_sharpness を下回るとき match は None を返さなければならない。

期待値は計画書「実測で確定させた数値」節に従う（OpenCV 4.12 / numpy 2.2）。
サブピクセル復元は放物線当てはめの pixel-locking バイアスにより最大 0.11px
の誤差が残るので、許容は abs=0.15 px とする（0.1px 精度は達成できない）。
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
    centered_roi,
)
from pcbasm.vision import Offset

PPM = 10.0  # pixel/mm
SIZE = 400  # 合成エッジマスクの一辺 (px)
ROI = (100, 100, 300, 300)  # 画像中心の 200px 角 ROI
WINDOW_PX = 20  # 既定 search_window 2.0mm * 10 px/mm


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


def _blank() -> np.ndarray:
    """400x400 の空エッジマスク."""
    return np.zeros((SIZE, SIZE), dtype=np.uint8)


def _ring(shift_x: float = 0.0, shift_y: float = 0.0, half: int = 50) -> np.ndarray:
    """画像中心に置いた正方リング（両方向に拘束がある）を指定 px ずらす."""
    mask = _blank()
    cx, cy = round(200 + shift_x), round(200 + shift_y)
    cv2.rectangle(mask, (cx - half, cy - half), (cx + half, cy + half), 255, 1)
    return mask


def _four_circles(shift_x: float = 0.0, shift_y: float = 0.0) -> np.ndarray:
    """1/16px 精度（cv2 の shift=4）で描いた円 4 個をサブピクセル量ずらす.

    整数 px 格子へ丸めた図形では非整数ずれを合成できないため、 サブピクセル復元の検証にはアンチエイリアスされない shift
    描画を使う。
    """
    mask = _blank()
    for offset_x, offset_y in ((-60, -60), (60, -60), (-60, 60), (60, 60)):
        cv2.circle(
            mask,
            (
                round((200 + offset_x + shift_x) * 16),
                round((200 + offset_y + shift_y) * 16),
            ),
            30 * 16,
            255,
            1,
            shift=4,
        )
    return mask


def _horizontal_line(x0: int, x1: int, y: int) -> np.ndarray:
    """水平線 1 本だけのエッジマスク（x 方向に拘束が無い＝開口問題）."""
    mask = _blank()
    cv2.line(mask, (x0, y), (x1, y), 255, 1)
    return mask


def _diagonal_line(
    shift_x: int = 0, shift_y: int = 0, *, angle: int = 45
) -> np.ndarray:
    """斜め線 1 本だけのエッジマスク（線方向に拘束が無い＝斜めの開口問題）.

    両端は ROI の内側（120..280）に収める。探索領域を縦断させると最小コスト
    位置が探索窓の端に張り付いて別の guard（窓端 → None）で先に落ちてしまい、
    sharpness の当てはめ自体を検証できないため。
    """
    mask = _blank()
    if angle == 45:
        p0, p1 = (120, 120), (280, 280)
    else:  # 135 度（反対向きの傾き。二次形式の交差項の符号が逆になる）
        p0, p1 = (120, 280), (280, 120)
    cv2.line(
        mask,
        (p0[0] + shift_x, p0[1] + shift_y),
        (p1[0] + shift_x, p1[1] + shift_y),
        255,
        1,
    )
    return mask


def _diamond(shift_x: int = 0, shift_y: int = 0, radius: int = 60) -> np.ndarray:
    """45 度回した正方リング（斜めエッジだけで両方向を拘束する等方な図形）."""
    mask = _blank()
    cx, cy = 200 + shift_x, 200 + shift_y
    points = np.array(
        [[cx, cy - radius], [cx + radius, cy], [cx, cy + radius], [cx - radius, cy]],
        dtype=np.int32,
    ).reshape(-1, 1, 2)
    cv2.polylines(mask, [points], isClosed=True, color=255, thickness=1)
    return mask


class TestCenteredRoi:
    """centered_roi の中心配置・クランプ・引数検証."""

    def test_returns_centered_square_of_the_requested_size(self):
        """画像中心に一辺 size_px の正方形を取る."""
        roi = centered_roi((1280, 720), 400)

        assert roi == (440, 160, 840, 560)

    @pytest.mark.parametrize("size_px", [200, 201])
    def test_size_is_exact_for_even_and_odd_sizes(self, size_px: int):
        """偶数・奇数どちらの一辺でも幅と高さは size_px ちょうどになる."""
        x0, y0, x1, y1 = centered_roi((640, 480), size_px)

        assert x1 - x0 == size_px
        assert y1 - y0 == size_px
        # 中心は画像中心から半端な丸め分（<1px）しかずれない
        assert abs((x0 + x1) / 2 - 320.0) <= 0.5
        assert abs((y0 + y1) / 2 - 240.0) <= 0.5

    def test_size_larger_than_frame_is_clamped_to_the_short_side(self):
        """size_px が画像より大きい場合は短辺へクランプする."""
        assert centered_roi((640, 480), 900) == (80, 0, 560, 480)

    def test_size_below_one_raises_value_error(self):
        with pytest.raises(ValueError, match="size_px"):
            centered_roi((640, 480), 0)


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

    def test_board_to_pixel_affine_reproduces_pixel_of(self):
        """公開したアフィンが pixel_of と一致する（線分の一括投影用）.

        ``pixels = coords @ matrix.T + shift``。plan_alignment_regions は
        数千頂点を 1 回の行列積で投影するためにこれを使う。
        """
        projector = _projector(
            [],
            board_transform=Shift(2.0, 1.0),
            offset_transform=Rotation(90.0),
            image_size=(320, 200),
        )
        stage_xy = Point2d(1.0, 2.0)

        matrix, shift = projector.board_to_pixel_affine(stage_xy)

        coords = np.array([[1.0, 0.5], [-3.0, 4.0], [0.0, 0.0]])
        pixels = coords @ matrix.T + shift
        for (x, y), row in zip(coords, pixels, strict=True):
            want = projector.pixel_of(Point2d(float(x), float(y)), stage_xy)
            assert row[0] == pytest.approx(want.x, abs=1e-9)
            assert row[1] == pytest.approx(want.y, abs=1e-9)

    def test_board_to_pixel_affine_matrix_is_independent_of_stage(self):
        """stage_xy は shift だけを動かす（matrix は不変）.

        plan_alignment_regions が参照アンカー 1 点でアフィンを取り、全候補を 同じ行列で採点できる根拠。
        """
        projector = _projector([], board_transform=Shift(2.0, 1.0))

        matrix_a, shift_a = projector.board_to_pixel_affine(Point2d(0.0, 0.0))
        matrix_b, shift_b = projector.board_to_pixel_affine(Point2d(7.0, -3.0))

        assert np.allclose(matrix_a, matrix_b)
        assert not np.allclose(shift_a, shift_b)

    def test_polygons_property_exposes_projection_targets(self):
        """Polygons が投影対象の銅箔（board 座標）をそのまま返す."""
        polygons = [_square(0.0, 0.0, 2.0), _square(5.0, 5.0, 1.0)]

        exposed = _projector(polygons).polygons

        assert isinstance(exposed, tuple)
        assert list(exposed) == polygons


class TestCopperProjectorWithCorrection:
    """with_correction: 機械座標の補正を board 変換の後段へ挿した投影器.

    計画書 region-affine-correction.md「実装ステップ 1」。反復計測（累積変位で
    投影を補正して再照合）と補正巡回（board_tour）が同じ 1 本を使う。
    """

    POLYGONS = [_square(0.0, 0.0, 2.0), _square(5.0, 5.0, 1.0)]

    def test_projects_with_the_composed_board_transform(self):
        """pixel_of が Compose([board_transform, correction]) 投影になる.

        非可換な board_transform=Shift と correction=Rotation の組で合成順序 （T_b
        を先、補正を後）を固定する。順序が逆だと補正が board 座標側に 効いてしまい、機械座標の補正という意味が崩れる。
        """
        board_transform = Shift(5.0, -1.0)
        correction = Rotation(90.0)
        projector = _projector(self.POLYGONS, board_transform=board_transform)
        reference = _projector(
            self.POLYGONS, board_transform=Compose([board_transform, correction])
        )

        corrected = projector.with_correction(correction)

        for board_point, stage_xy in [
            (Point2d(0.0, 0.0), Point2d(0.0, 0.0)),
            (Point2d(1.0, 0.5), Point2d(3.0, 2.0)),
        ]:
            got = corrected.pixel_of(board_point, stage_xy)
            want = reference.pixel_of(board_point, stage_xy)
            assert got.x == pytest.approx(want.x, abs=1e-9)
            assert got.y == pytest.approx(want.y, abs=1e-9)

    def test_shift_correction_moves_the_projection_by_the_shift(self):
        """並進補正 Shift(d) は投影を −ppm*d だけ動かす（反復計測の中身）.

        投影公式 pixel = center + ppm*(s − T_b(b) − d) の d の効き方を直接ピンする。
        これが効かないと 2 パス目が同じ変位を再び測って二重計上する。
        """
        projector = _projector([_square(0.0, 0.0, 2.0)])
        stage_xy = Point2d(0.0, 0.0)
        base = projector.pixel_of(Point2d(0.0, 0.0), stage_xy)

        corrected = projector.with_correction(Shift(0.3, -0.2))

        moved = corrected.pixel_of(Point2d(0.0, 0.0), stage_xy)
        assert moved.x == pytest.approx(base.x - PPM * 0.3, abs=1e-9)
        assert moved.y == pytest.approx(base.y + PPM * 0.2, abs=1e-9)

    def test_inherits_polygons_and_pixel_scale_and_image_size(self):
        """ポリゴン・pixel/mm・画像サイズを引き継ぐ（設定の取りこぼし防止）."""
        projector = _projector(self.POLYGONS, image_size=(320, 200))

        corrected = projector.with_correction(Shift(0.0, 0.0))

        assert list(corrected.polygons) == self.POLYGONS
        # pixel/mm と image_size は private なので投影結果で確認する
        assert corrected.project(Point2d(0.0, 0.0)).edge_mask.shape == (200, 320)
        origin = corrected.pixel_of(Point2d(0.0, 0.0), Point2d(0.0, 0.0))
        unit_x = corrected.pixel_of(Point2d(1.0, 0.0), Point2d(0.0, 0.0))
        assert abs(unit_x.x - origin.x) == pytest.approx(PPM, abs=1e-9)

    def test_original_projector_is_unchanged(self):
        """元の投影器は変わらない（不変性）.

        反復計測は同じ RegionAligner の中でパスごとに補正を差し替えるので、 元の投影器が汚れると 3
        パス目以降が壊れる。
        """
        projector = _projector(self.POLYGONS)
        before = projector.pixel_of(Point2d(0.0, 0.0), Point2d(0.0, 0.0))

        projector.with_correction(Shift(10.0, 10.0))

        after = projector.pixel_of(Point2d(0.0, 0.0), Point2d(0.0, 0.0))
        assert after.x == pytest.approx(before.x, abs=1e-12)
        assert after.y == pytest.approx(before.y, abs=1e-12)


class TestCopperEdgeMatcherTranslation:
    """CopperEdgeMatcher.match の並進復元（正常系）."""

    @pytest.fixture
    def matcher(self) -> CopperEdgeMatcher:
        """標準 matcher (10 px/mm, 窓 2mm = 20px)."""
        return CopperEdgeMatcher(pixel_per_mm=PPM)

    def test_window_px_is_rounded_from_search_window_mm(self):
        """window_px = round(search_window_mm * pixel_per_mm)."""
        assert CopperEdgeMatcher(pixel_per_mm=PPM).window_px == WINDOW_PX
        assert (
            CopperEdgeMatcher(pixel_per_mm=30.225, search_window_mm=1.4).window_px == 42
        )

    def test_match_recovers_known_integer_shift(self, matcher: CopperEdgeMatcher):
        """観測が想定から (+7, -4)px ずれた正方リング → offset.px = (7, -4).

        符号ピン: offset は「観測 - 想定」で、想定エッジを offset だけ
        動かすと観測に重なる向き。整数ずれでは補間量が 0 になる。
        """
        match = matcher.match(_ring(7, -4), _ring(), ROI)

        assert match is not None
        assert match.offset.px.x == pytest.approx(7.0, abs=0.05)
        assert match.offset.px.y == pytest.approx(-4.0, abs=0.05)

    @pytest.mark.parametrize(
        ("shift_x", "shift_y"),
        [(2.4, -1.2), (-1.6, 0.8), (0.5, 0.5)],
    )
    def test_match_recovers_subpixel_shift(
        self, matcher: CopperEdgeMatcher, shift_x: float, shift_y: float
    ):
        """1/16px 精度で描いた円 4 個の非整数ずれを 0.15px 以内で復元する.

        計画書「サブピクセル復元精度」: 放物線当てはめの pixel-locking で 最大 0.110px の系統誤差が残るので
        0.1px 精度は要求しない。 整数 px の探索位置しか返さない旧実装ではこのテストは通らない。

        シフト値は許容 0.15px に余裕がある 3 点を選んである（同じ円フィクスチャを ±1px の
        0.1px 刻みで振ると pixel-locking の最悪誤差は 0.127px まで伸びる）。網羅的に増やすなら
        許容も緩める必要がある。
        """
        match = matcher.match(_four_circles(shift_x, shift_y), _four_circles(), ROI)

        assert match is not None
        assert match.offset.px.x == pytest.approx(shift_x, abs=0.15)
        assert match.offset.px.y == pytest.approx(shift_y, abs=0.15)

    def test_subpixel_shift_reports_nonzero_rms_distance(
        self, matcher: CopperEdgeMatcher
    ):
        """サブピクセルずれでは rms_distance_px が 0 に飽和しない.

        旧 mean_distance_px は「残差 < 0.5px は 2 つのラスタライズが完全一致 して厳密に
        0」という飽和値だった（0 = 誤差ゼロではない）。距離の二乗を 補間した最小コストから導く新実装では実残差が出る（実測
        0.485〜0.520）。
        """
        match = matcher.match(_four_circles(2.4, -1.2), _four_circles(), ROI)

        assert match is not None
        assert match.rms_distance_px > 0.2

    def test_exact_match_reports_zero_but_non_negative_rms(
        self, matcher: CopperEdgeMatcher
    ):
        """完全一致でのみ rms ≈ 0。FFT 丸めでも負値にはならない.

        生の最小コストは FFT 丸めで負（実測 −8.9e-08）になり得るので、 実装は sqrt の前に max(c*, 0)
        でクリップしなければならない。
        """
        match = matcher.match(_ring(), _ring(), ROI)

        assert match is not None
        assert match.offset.px.x == pytest.approx(0.0, abs=0.05)
        assert match.offset.px.y == pytest.approx(0.0, abs=0.05)
        assert match.rms_distance_px >= 0.0
        assert match.rms_distance_px == pytest.approx(0.0, abs=1e-3)

    def test_match_recovers_shift_with_partially_missing_observed_edges(
        self, matcher: CopperEdgeMatcher
    ):
        """観測エッジの下半分が欠損していてもずれを復元できる."""
        observed = _ring(5, 3)
        observed[260:, :] = 0  # 下辺と縦辺の下部が消える

        match = matcher.match(observed, _ring(), ROI)

        assert match is not None
        assert match.offset.px.x == pytest.approx(5.0, abs=0.5)
        assert match.offset.px.y == pytest.approx(3.0, abs=0.5)

    def test_match_recovers_shift_despite_noise_edges(self, matcher: CopperEdgeMatcher):
        """観測にノイズエッジ画素が混ざってもずれを復元できる."""
        observed = _ring(6, -2)
        rng = np.random.default_rng(seed=42)
        noise = rng.integers(0, SIZE, size=(40, 2))
        observed[noise[:, 0], noise[:, 1]] = 255

        match = matcher.match(observed, _ring(), ROI)

        assert match is not None
        assert match.offset.px.x == pytest.approx(6.0, abs=0.5)
        assert match.offset.px.y == pytest.approx(-2.0, abs=0.5)

    def test_match_uses_only_template_inside_roi(self):
        """ROI 外の逆ずれ構造が照合結果に影響しない."""
        matcher = CopperEdgeMatcher(pixel_per_mm=PPM, search_window_mm=1.0)
        expected = _blank()
        observed = _blank()
        # ROI 内: (+4, +2)px ずれた正方リング
        cv2.rectangle(expected, (150, 150), (250, 250), 255, 1)
        cv2.rectangle(observed, (154, 152), (254, 252), 255, 1)
        # ROI + 窓 (10px) の外: 逆向き (-8, -6) のずれを示唆する構造
        cv2.rectangle(expected, (10, 10), (70, 70), 255, 1)
        cv2.rectangle(expected, (25, 25), (55, 55), 255, 1)
        cv2.rectangle(observed, (2, 4), (62, 64), 255, 1)
        cv2.rectangle(observed, (17, 19), (47, 49), 255, 1)

        match = matcher.match(observed, expected, ROI)

        assert match is not None
        assert match.offset.px.x == pytest.approx(4.0, abs=0.5)
        assert match.offset.px.y == pytest.approx(2.0, abs=0.5)

    def test_match_is_stable_with_edges_crossing_roi_boundary(
        self, matcher: CopperEdgeMatcher
    ):
        """ROI 境界を横断する長いエッジがあっても既知ずれを復元する.

        エッジ線マスクの矩形切出しは新規画素を生まない（クリップ偽エッジ ゼロ）ことのピン。フレーム全幅の水平線が ROI を横断する。
        """
        expected = _ring()
        cv2.line(expected, (0, 200), (SIZE - 1, 200), 255, 1)
        observed = _ring(3, -2)
        cv2.line(observed, (0, 198), (SIZE - 1, 198), 255, 1)

        match = matcher.match(observed, expected, ROI)

        assert match is not None
        assert match.offset.px.x == pytest.approx(3.0, abs=0.5)
        assert match.offset.px.y == pytest.approx(-2.0, abs=0.5)


class TestCopperEdgeMatcherRejection:
    """CopperEdgeMatcher.match が None を返す条件（今回のバグの直接ピン）."""

    @pytest.fixture
    def matcher(self) -> CopperEdgeMatcher:
        return CopperEdgeMatcher(pixel_per_mm=PPM)

    def test_empty_observed_or_expected_mask_returns_none(
        self, matcher: CopperEdgeMatcher
    ):
        """観測エッジが空、または ROI 内の想定エッジが空なら None."""
        assert matcher.match(_blank(), _ring(), ROI) is None
        assert matcher.match(_ring(), _blank(), ROI) is None

    def test_expected_edges_only_outside_roi_returns_none(
        self, matcher: CopperEdgeMatcher
    ):
        """ROI 外にしか想定エッジが無い場合は偽照合せず None."""
        expected = _blank()
        cv2.rectangle(expected, (5, 5), (60, 60), 255, 1)

        assert matcher.match(_ring(), expected, ROI) is None

    def test_flat_cost_surface_from_one_directional_edges_returns_none(
        self, matcher: CopperEdgeMatcher
    ):
        """開口問題の棄却: 水平線だけの領域は None（本タスクの中核）.

        観測・想定とも探索領域を横断する水平線 1 本。x 方向のコストは完全に 平坦（実測 sharpness =
        0.0000）で、平坦域のタイブレークを決めるのは matchTemplate の FFT 丸め誤差でしかない。旧実装は
        mean_distance_px ≈ 0（＝誤差ゼロに見える）のまま dx に 0.6mm 級の
        デタラメを返し、これが部品ごとにバラバラなずれの主因だった。
        """
        expected = _horizontal_line(85, 315, 200)
        observed = _horizontal_line(85, 315, 202)

        assert matcher.match(observed, expected, ROI) is None

    def test_flat_cost_surface_is_reported_as_zero_sharpness(self):
        """棄却の根拠が sharpness ≈ 0 であることをピンする.

        min_sharpness=0.0 なら EdgeMatch は返るが、その sharpness は 0。
        棄却の理由が「探索窓の端に張り付いた」等ではなく拘束不足であること。
        """
        matcher = CopperEdgeMatcher(pixel_per_mm=PPM, min_sharpness=0.0)

        match = matcher.match(
            _horizontal_line(85, 315, 202), _horizontal_line(85, 315, 200), ROI
        )

        assert match is not None
        assert match.sharpness == pytest.approx(0.0, abs=1e-6)
        assert match.offset.px.y == pytest.approx(2.0, abs=0.15)  # 拘束のある軸は正しい

    def test_weakly_constrained_edges_are_rejected_by_default_threshold(
        self, matcher: CopperEdgeMatcher
    ):
        """端点しか x を拘束しない水平線（実測 sharpness 0.07）も棄却する.

        既定 min_sharpness=0.15 は「棄却側 <= 0.08 / 採択側 >= 0.5」の間にある。
        """
        expected = _horizontal_line(105, 295, 200)
        observed = _horizontal_line(105, 295, 202)

        assert matcher.match(observed, expected, ROI) is None

        permissive = CopperEdgeMatcher(pixel_per_mm=PPM, min_sharpness=0.0)
        weak = permissive.match(observed, expected, ROI)
        assert weak is not None
        assert weak.sharpness < 0.1

    @pytest.mark.parametrize("angle", [45, 135])
    def test_diagonal_one_directional_edges_are_rejected(self, angle: int):
        """斜め 1 方向のエッジだけの領域も棄却する（二次形式の交差項のピン）.

        水平・垂直の一方向エッジでは 3x3 コスト近傍の Σxy·c が 0 になるため、
        ヘッセの非対角成分（交差項）が誤っていても棄却できてしまう。弱軸が
        45 度を向くこのケースだけが交差項を通る: 実装の ``hxy`` を半分に
        壊すと sharpness が 0.0 → 0.30 に跳ね上がり、既定閾値 0.15 を突破して
        「拘束のある領域」として誤って採択される。
        """
        strict = CopperEdgeMatcher(pixel_per_mm=PPM)
        permissive = CopperEdgeMatcher(pixel_per_mm=PPM, min_sharpness=0.0)
        observed = _diagonal_line(2, -2, angle=angle)
        expected = _diagonal_line(angle=angle)

        assert strict.match(observed, expected, ROI) is None

        weak = permissive.match(observed, expected, ROI)
        assert weak is not None
        assert weak.sharpness < 0.05  # 線方向は完全に平坦（実測 0.0000）

    def test_diagonal_ring_is_accepted(self):
        """斜めエッジだけで構成された等方な図形（45 度回した正方リング）は採択する.

        交差項が効くケースを一律に落としてしまわないことのピン。実測 sharpness は 0.577（軸平行の正方リング 0.706
        より低いが閾値の 4 倍）。
        """
        matcher = CopperEdgeMatcher(pixel_per_mm=PPM)

        match = matcher.match(_diamond(3, -2), _diamond(), ROI)

        assert match is not None
        assert match.sharpness > 0.5
        assert match.offset.px.x == pytest.approx(3.0, abs=0.1)
        assert match.offset.px.y == pytest.approx(-2.0, abs=0.1)

    def test_shift_beyond_search_window_returns_none(self, matcher: CopperEdgeMatcher):
        """探索窓（20px）を超える 30px ずれは窓端に張り付くので None.

        真の最小が窓の外にあり 3x3 近傍も取れない。max_correction (1.0mm) は search_window
        (2.0mm) より小さいので、正常な照合はここに来ない。
        """
        assert matcher.match(_ring(30, 0), _ring(), ROI) is None

    @pytest.mark.parametrize(
        ("min_sharpness", "accepted"),
        [(0.0, True), (0.5, True), (0.9, False)],
    )
    def test_min_sharpness_threshold_decides_acceptance(
        self, min_sharpness: float, accepted: bool
    ):
        """同じ入力でも min_sharpness の設定で採択/棄却が切り替わる.

        正方リング（両方向に等方な拘束）の実測 sharpness は 0.7059。
        """
        matcher = CopperEdgeMatcher(pixel_per_mm=PPM, min_sharpness=min_sharpness)

        match = matcher.match(_ring(2, 3), _ring(), ROI)

        assert (match is not None) is accepted

    def test_sharpness_ranks_isotropic_above_one_directional(self):
        """Sharpness の順序: 正方リング（> 0.5）> 水平線のみ（< 0.1）.

        sharpness は「弱軸方向へ 1px ずらしたときの RMS 距離の増分 [px]」で、 等方な正方リングでは
        sqrt(1/2) = 0.707 が上限に近い。
        """
        matcher = CopperEdgeMatcher(pixel_per_mm=PPM, min_sharpness=0.0)

        ring_match = matcher.match(_ring(2, 3), _ring(), ROI)
        line_match = matcher.match(
            _horizontal_line(105, 295, 202), _horizontal_line(105, 295, 200), ROI
        )

        assert ring_match is not None
        assert line_match is not None
        assert ring_match.sharpness > 0.5
        assert line_match.sharpness < 0.1
        assert ring_match.sharpness > line_match.sharpness


class TestEdgeMatch:
    """EdgeMatch の camera_transform 契約のテスト."""

    def test_camera_transform_is_pure_shift_by_offset(self):
        """camera_transform = o ↦ o + d（d = offset.mm）.

        照合は並進のみなので camera_transform は Shift.from_point(offset.mm) と
        等価。回転中心という概念が無く、どの点でも同じ変位ベクトルになる。
        """
        match = EdgeMatch(
            offset=Offset(px=Point2d(12.0, -8.0), pixel_per_mm=PPM),
            rms_distance_px=0.5,
            sharpness=0.7,
        )

        transform = match.camera_transform

        d = Point2d(1.2, -0.8)  # offset.mm
        for point in (Point2d(0.0, 0.0), Point2d(2.0, 1.0), Point2d(-30.0, 40.0)):
            mapped = transform.apply(point)
            assert mapped.x == pytest.approx(point.x + d.x, abs=1e-9)
            assert mapped.y == pytest.approx(point.y + d.y, abs=1e-9)
