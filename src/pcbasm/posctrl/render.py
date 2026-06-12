"""位置合わせ・巡回の表示フレームを合成する純粋関数群（cv2 GUI 非依存）.

cv2 の描画 API は使うが、ウィンドウ表示（imshow / waitKey）は行わない。 合成結果の Image は
FrameSink（scripts の cv2 ウィンドウ、webui のプレビュー オーバーライド等）へ渡して表示する。
"""

import math
from collections.abc import Sequence

import cv2
import numpy as np
from shapely import Polygon

from pcbasm.config import PadAlign
from pcbasm.geometry import Point2d
from pcbasm.posctrl.copper import CopperProjector, PixelRect
from pcbasm.vision import (
    CopperEdgeDetector,
    Image,
    ImageArray,
    draw_crosshair,
    draw_overlay,
)

_EXPECTED_COLOR = (0, 0, 255)  # 想定エッジの表示色 (BGR: 赤)
_DETECTED_COLOR = (0, 255, 0)  # 検出エッジの表示色 (BGR: 緑)
_ROI_COLOR = (255, 255, 255)  # ROI枠の表示色 (BGR: 白)
_FILL_COLOR = (0, 0, 255)  # 対象pad薄塗りの色 (BGR)
_FILL_ALPHA = 0.35
_LABEL_COLOR = (0, 255, 0)  # ラベル文字色 (BGR: 緑)


def render_label(image: Image, crop_size: tuple[int, int], label: str) -> Image:
    """十字線・関心領域・緑ラベル文字を重ねたフレームを合成する（非破壊）."""
    img = draw_overlay(image, crop_size).numpy()
    cv2.putText(img, label, (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, _LABEL_COLOR, 2)
    return Image(img)


def render_edge_match(
    image: Image, edges: ImageArray, edge_mask: ImageArray, roi: PixelRect
) -> Image:
    """ROI枠と想定（赤）・検出（緑）エッジ、中心十字を重ねて合成する（非破壊）.

    Args:
        image: 元画像
        edges: 検出エッジマスク (uint8, 0/255)。画像と同サイズ
        edge_mask: 想定エッジマスク (uint8, 0/255)。画像と同サイズ
        roi: 照合に使う ROI 矩形（全画面 px）
    """
    x0, y0, x1, y1 = roi
    display = image.numpy().copy()
    roi_view = display[y0:y1, x0:x1]
    roi_view[edge_mask[y0:y1, x0:x1] > 0] = _EXPECTED_COLOR
    roi_view[edges[y0:y1, x0:x1] > 0] = _DETECTED_COLOR
    cv2.rectangle(display, (x0, y0), (x1 - 1, y1 - 1), _ROI_COLOR, 1)
    draw_crosshair(display)
    return Image(display)


class PadResultRenderer:
    """Pad 照合結果 overlay の単一フレーム合成器.

    塗布対象（ペースト pad 領域）の薄塗り + 想定している銅箔の輪郭（赤）+
    検出された銅箔輪郭（緑）+ 中心十字 + テキスト行を合成する。
    projection / ROI / 薄塗りマスクは position 固定で ``__init__`` で 1 回だけ
    計算し、``render()`` は毎フレームのエッジ検出と合成のみ行う。
    """

    def __init__(
        self,
        *,
        projector: CopperProjector,
        edge_detector: CopperEdgeDetector,
        roi_polygons: Sequence[Polygon],
        paste_polygons: Sequence[Polygon],
        pad_align: PadAlign,
        position: Point2d,
    ) -> None:
        """PadResultRenderer を初期化する.

        Args:
            projector: 設計銅箔の投影器
            edge_detector: 銅箔エッジ検出器
            roi_polygons: ROI を決める実銅箔ポリゴン（board 座標、mm）。
                空の場合は画像中心に min_roi サイズの ROI を取る
            paste_polygons: 薄塗りするペースト開口ポリゴン（board 座標、mm）
            pad_align: ROI マージン・最小辺長の設定
            position: 表示位置（機械座標、mm）。投影と ROI をこの位置で固定する
        """
        self._edge_detector = edge_detector
        projection = projector.project(position)
        if roi_polygons:
            x0, y0, x1, y1 = projector.roi_of(
                list(roi_polygons),
                position,
                margin_mm=pad_align.roi_margin,
                min_size_mm=pad_align.min_roi,
            )
        else:
            x0, y0, x1, y1 = _centered_roi(
                projector, position, projection.edge_mask.shape, pad_align.min_roi
            )
        self._roi = np.zeros(projection.edge_mask.shape, dtype=bool)
        self._roi[y0:y1, x0:x1] = True
        self._expected = (projection.edge_mask > 0) & self._roi

        # ペーストpad領域（塗布対象）を投影して薄塗りマスクを作る
        fill_mask = np.zeros(projection.edge_mask.shape, dtype=np.uint8)
        for paste in paste_polygons:
            pixels = [
                projector.pixel_of(Point2d(float(x), float(y)), position)
                for x, y in paste.exterior.coords
            ]
            points = np.array(
                [[round(p.x), round(p.y)] for p in pixels], dtype=np.int32
            )
            cv2.fillPoly(fill_mask, [points], 255)
        self._fill = fill_mask > 0

    def render(self, image: Image, lines: Sequence[str]) -> Image:
        """エッジ検出と overlay 合成を行ったフレームを返す（非破壊）."""
        detected = (self._edge_detector.detect_edges(image) > 0) & self._roi

        display = image.numpy().copy()
        color_layer = np.zeros_like(display)
        color_layer[:] = _FILL_COLOR
        blended = cv2.addWeighted(
            display, 1.0 - _FILL_ALPHA, color_layer, _FILL_ALPHA, 0.0
        )
        display[self._fill] = blended[self._fill]
        display[self._expected] = _EXPECTED_COLOR
        display[detected] = _DETECTED_COLOR
        draw_crosshair(display)

        for i, line in enumerate(lines):
            position = (10, 25 + i * 25)
            cv2.putText(
                display, line, position, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3
            )
            cv2.putText(
                display,
                line,
                position,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                1,
            )
        return Image(display)


def _centered_roi(
    projector: CopperProjector,
    position: Point2d,
    mask_shape: tuple[int, ...],
    min_roi_mm: float,
) -> PixelRect:
    """画像中心に min_roi サイズの ROI 矩形を取る（roi_polygons が空の場合用）.

    pixel/mm は board 単位ベクトルの投影長から導出する（board 変換は ほぼ剛体のため誤差は無視できる）。
    """
    origin = projector.pixel_of(Point2d(0.0, 0.0), position)
    unit_x = projector.pixel_of(Point2d(1.0, 0.0), position)
    min_px = min_roi_mm * (unit_x - origin).norm
    height, width = mask_shape[:2]
    cx, cy = width / 2, height / 2
    return (
        max(0, math.floor(cx - min_px / 2)),
        max(0, math.floor(cy - min_px / 2)),
        min(width, math.ceil(cx + min_px / 2)),
        min(height, math.ceil(cy + min_px / 2)),
    )
