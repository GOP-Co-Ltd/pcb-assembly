"""Posctrl/render の仕様テスト.

計画書 memory/agents/implementation-planner/webui-phase4.md「§1 pcbasm 注入点の
再設計」に基づく。render.py は cv2 GUI（imshow / waitKey）に依存しない純粋な
画像合成関数群で、合成結果は FrameSink（scripts の cv2 ウィンドウ、webui の
プレビューオーバーライド）へ渡される:

- render_label: draw_overlay + 緑ラベル文字（tour.py の合成部分の昇格）
- render_edge_match: ROI 枠（白）+ 想定エッジ（赤）+ 検出エッジ（緑）+
  中心十字（pad.py CopperPadObserver._show の合成部分の昇格）
- PadResultRenderer: pad 照合結果 overlay の単一フレーム合成器
  （scripts/posctrl/board_tour.py の _show_pad_result の昇格。
  投影・ROI・薄塗りマスクは __init__ で position 固定）

投影のイディオム（銅箔 ±4mm・Identity 変換・ppm=10 → (600,320)-(680,400) px）
は test_alignment.py を踏襲する。
"""

import cv2
import numpy as np
import pytest
import shapely

from pcbasm.geometry import Identity, Point2d
from pcbasm.posctrl.copper import CopperProjector, PixelRect, centered_roi
from pcbasm.posctrl.render import PadResultRenderer, render_edge_match, render_label
from pcbasm.vision import CopperEdgeDetector, Image

WIDTH, HEIGHT = 1280, 720  # カメラフレームサイズ (px)
PPM = 10.0  # pixel/mm

RED = (0, 0, 255)
GREEN = (0, 255, 0)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def _count_exact(arr: np.ndarray, color: tuple[int, int, int]) -> int:
    """指定 BGR 色に完全一致する画素数."""
    return int((arr == np.array(color, dtype=np.uint8)).all(axis=-1).sum())


class TestRenderLabel:
    """render_label の合成と非破壊性のテスト."""

    @pytest.fixture
    def image(self) -> Image:
        return Image(np.zeros((200, 300, 3), dtype=np.uint8))

    def test_keeps_size_and_does_not_mutate_input(self, image: Image):
        """出力は入力と同サイズで、入力配列は変更されない."""
        before = image.numpy().copy()

        out = render_label(image, (100, 100), "Corner: TL")

        assert out.size == image.size
        assert np.array_equal(image.numpy(), before)

    def test_draws_crosshair_crop_rect_and_green_label(self, image: Image):
        """十字線・crop 枠に加えてラベル文字が緑で描かれる.

        ラベル有無で緑画素数が増えることでラベル描画をピンする （文字グリフの形状自体は検証しない）。
        """
        without_label = render_label(image, (100, 100), "")
        with_label = render_label(image, (100, 100), "Corner: TL")

        base_green = _count_exact(without_label.numpy(), GREEN)
        labeled_green = _count_exact(with_label.numpy(), GREEN)
        assert base_green > 50  # 十字線 + crop 枠
        assert labeled_green > base_green  # ラベル文字の分が加算される


class TestRenderEdgeMatch:
    """render_edge_match の ROI 限定合成のテスト（合成エッジマスクでピン）."""

    SIZE = 200
    ROI = (40, 40, 160, 160)

    @pytest.fixture
    def image(self) -> Image:
        return Image(np.zeros((self.SIZE, self.SIZE, 3), dtype=np.uint8))

    @pytest.fixture
    def edge_mask(self) -> np.ndarray:
        """想定エッジ: y=80 の水平線（ROI を左右にはみ出す）."""
        mask = np.zeros((self.SIZE, self.SIZE), dtype=np.uint8)
        mask[80, 20:180] = 255
        return mask

    @pytest.fixture
    def edges(self) -> np.ndarray:
        """検出エッジ: y=120 の水平線（ROI を左右にはみ出す）."""
        mask = np.zeros((self.SIZE, self.SIZE), dtype=np.uint8)
        mask[120, 20:180] = 255
        return mask

    def test_colors_expected_red_and_detected_green_inside_roi(
        self, image: Image, edges: np.ndarray, edge_mask: np.ndarray
    ):
        out = render_edge_match(image, edges, edge_mask, self.ROI).numpy()

        # 検証画素は中心十字（col 100±30）と重ならない col 60 を使う
        assert tuple(out[80, 60]) == RED  # 想定エッジ
        assert tuple(out[120, 60]) == GREEN  # 検出エッジ
        assert tuple(out[40, 60]) == WHITE  # ROI 枠
        assert tuple(out[100, 100]) == GREEN  # 中心十字

    def test_masks_outside_roi_are_not_colored(
        self, image: Image, edges: np.ndarray, edge_mask: np.ndarray
    ):
        """ROI の外側ではエッジマスクが乗らない（照合範囲の可視化に限定）."""
        out = render_edge_match(image, edges, edge_mask, self.ROI).numpy()

        assert tuple(out[80, 30]) == BLACK  # 想定エッジの ROI 外部分
        assert tuple(out[120, 170]) == BLACK  # 検出エッジの ROI 外部分

    def test_does_not_mutate_inputs(
        self, image: Image, edges: np.ndarray, edge_mask: np.ndarray
    ):
        before_image = image.numpy().copy()
        before_edges = edges.copy()
        before_mask = edge_mask.copy()

        render_edge_match(image, edges, edge_mask, self.ROI)

        assert np.array_equal(image.numpy(), before_image)
        assert np.array_equal(edges, before_edges)
        assert np.array_equal(edge_mask, before_mask)


def _square(cx: float, cy: float, half: float) -> shapely.Polygon:
    """中心 (cx, cy)、半辺 half の正方形ポリゴンを作る."""
    return shapely.Polygon(
        [
            (cx - half, cy - half),
            (cx + half, cy - half),
            (cx + half, cy + half),
            (cx - half, cy + half),
        ]
    )


def _board_image(shift_x: int = 0, shift_y: int = 0) -> Image:
    """黒地に白矩形 (600,320)-(680,400) を指定 px ずらして描いた合成画像.

    銅箔 ±4mm 角（board 原点中心）・Identity 変換・position (0,0) のとき、 投影公式で銅箔は
    (600,320)-(680,400) px に投影される（test_alignment.py と同じシナリオ）。実
    CopperEdgeDetector の Canny で矩形境界がエッジ化 される入力。
    """
    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    cv2.rectangle(
        frame,
        (600 + shift_x, 320 + shift_y),
        (680 + shift_x, 400 + shift_y),
        (255, 255, 255),
        thickness=-1,
    )
    return Image(frame)


class TestPadResultRenderer:
    """PadResultRenderer の overlay 合成のテスト（実 projector / detector）.

    ROI は照合領域（RegionAlignmentSession.region_roi）をそのまま受け取る。 pad ごとの投影
    bbox からは決めない（領域単位の照合では pad 単位の ROI が 存在しないため）。
    """

    COPPER = _square(0.0, 0.0, 4.0)  # 実銅箔 ±4mm → (600,320)-(680,400) px
    PASTE = _square(0.0, 0.0, 0.5)  # ペースト開口 ±0.5mm → (635,355)-(645,365) px
    ROI = centered_roi((WIDTH, HEIGHT), 400)  # (440, 160, 840, 560)

    @staticmethod
    def _renderer(
        paste_polygons: tuple[shapely.Polygon, ...],
        roi: PixelRect | None = None,
    ) -> PadResultRenderer:
        projector = CopperProjector(
            polygons=[TestPadResultRenderer.COPPER],
            board_transform=Identity(),
            offset_transform=Identity(),
            pixel_per_mm=PPM,
            image_size=(WIDTH, HEIGHT),
        )
        return PadResultRenderer(
            projector=projector,
            edge_detector=CopperEdgeDetector(),
            roi=roi if roi is not None else TestPadResultRenderer.ROI,
            paste_polygons=paste_polygons,
            position=Point2d(0.0, 0.0),
        )

    def test_render_composes_fill_expected_contour_and_lines(self):
        """黒画像 → 薄塗り（ペースト開口）+ 想定輪郭（赤）+ テキスト行を合成.

        - 薄塗り: 黒地 × FILL_ALPHA=0.35 の赤 → R≈89（十字線を避けた画素で確認）
        - 想定輪郭: 投影銅箔境界 (600,320)-(680,400) 上の画素が純赤
        - lines: 白文字（黒縁取り + 白）が左上領域に乗る
        """
        renderer = self._renderer((self.PASTE,))
        image = Image(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))

        out = renderer.render(image, ["Region 1/4", "FAILED"]).numpy()

        fill_pixel = out[357, 638]  # ペースト開口内・十字線非干渉の画素
        assert int(fill_pixel[2]) == pytest.approx(89, abs=3)  # 0.35 * 255
        assert int(fill_pixel[0]) == 0
        assert int(fill_pixel[1]) == 0
        assert tuple(out[320, 640]) == RED  # 想定銅箔の上辺
        assert _count_exact(out[:60, :250], WHITE) > 0  # テキスト行の白文字

    def test_render_marks_detected_edges_green_inside_roi(self):
        """既知ずれの白矩形画像 → 検出エッジ（緑）が ROI 内に現れる."""
        renderer = self._renderer((self.PASTE,))

        out = renderer.render(_board_image(6, -4), ["Region 1/4"]).numpy()

        # 十字線（約 120 px）を大きく超える緑画素 = Canny 検出エッジの重畳
        assert _count_exact(out, GREEN) > 250

    def test_render_keeps_size_and_does_not_mutate_input(self):
        renderer = self._renderer((self.PASTE,))
        image = _board_image()
        before = image.numpy().copy()

        out = renderer.render(image, [])

        assert out.size == image.size
        assert np.array_equal(image.numpy(), before)

    def test_expected_contour_outside_the_given_roi_is_not_drawn(self):
        """渡された ROI の外にある想定銅箔輪郭は描かれない.

        overlay は照合に使った範囲だけを可視化する。ROI を小さく取ると 銅箔上辺 (row 320) は範囲外になる。
        """
        renderer = self._renderer((self.PASTE,), roi=centered_roi((WIDTH, HEIGHT), 30))
        image = Image(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))

        out = renderer.render(image, []).numpy()

        assert out.shape == (HEIGHT, WIDTH, 3)
        assert tuple(out[320, 640]) == BLACK  # 銅箔上辺は 30px 角 ROI の外
