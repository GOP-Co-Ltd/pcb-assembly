"""設計銅箔のpixel空間への投影と、観測エッジとの照合."""

import math
from collections.abc import Sequence

import attrs
import cv2
import numpy as np
from shapely import Polygon
from shapely.coords import CoordinateSequence

from pcbasm.geometry import Point2d, Shift, Transform
from pcbasm.vision import ImageArray, Offset

type _Bounds = tuple[float, float, float, float]
# ROI矩形 (x0, y0, x1, y1)。半開区間、全画面pixel座標
type PixelRect = tuple[int, int, int, int]


@attrs.frozen
class CopperProjection:
    """視野内の想定銅箔のpixel空間表現.

    Attributes:
        fill_mask: 穴を反映した塗り潰しマスク (uint8, 0/255)。overlay表示用
        edge_mask: polygon境界の1px線マスク (uint8, 0/255)。マッチング用
    """

    fill_mask: ImageArray = attrs.field(eq=False)
    edge_mask: ImageArray = attrs.field(eq=False)


@attrs.frozen
class EdgeMatch:
    """観測エッジと想定エッジの照合結果.

    Attributes:
        offset: 並進（観測 − 想定、px。.mm でmm）
        mean_distance_px: 想定エッジ1点あたりの平均chamfer距離 (px)。品質指標
    """

    offset: Offset
    mean_distance_px: float

    @property
    def camera_transform(self) -> Transform:
        """想定→観測のTransform: o ↦ o + d."""
        return Shift.from_point(self.offset.mm)


def _ring_to_pixels(
    coords: CoordinateSequence,
    matrix: ImageArray,
    shift: ImageArray,
) -> ImageArray:
    """リング座標列 (mm) をpixel座標のint32配列へ変換する."""
    points = np.asarray(coords, dtype=np.float64)
    pixels = points @ matrix.T + shift
    return np.round(pixels).astype(np.int32).reshape(-1, 1, 2)


def _bounds_overlap(a: _Bounds, b: _Bounds) -> bool:
    """2つのbbox (minx, miny, maxx, maxy) が重なるかを判定する."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


class CopperProjector:
    """設計銅箔ポリゴンをカメラpixel空間へ投影するクラス.

    投影公式は ``pixel(b, s) = image_center + ppm * R(s - T_b(b))``
    （s: ステージ位置, T_b: board変換, R: オフセット変換）。
    画像中心は全画面中心 (width/2, height/2) を基準とする。
    """

    def __init__(
        self,
        polygons: Sequence[Polygon],
        board_transform: Transform,
        offset_transform: Transform,
        pixel_per_mm: float,
        image_size: tuple[int, int],
    ) -> None:
        """CopperProjectorを初期化する.

        Args:
            polygons: 銅箔ポリゴン列（shapely、mm単位、board座標）
            board_transform: board座標→機械座標の変換
            offset_transform: 観測オフセット系から機械座標系への変換
            pixel_per_mm: pixel/mm比率
            image_size: 出力マスクのサイズ (width, height)
        """
        self._polygons = list(polygons)
        self._board_transform = board_transform
        self._offset_transform = offset_transform
        self._pixel_per_mm = pixel_per_mm
        self._image_size = image_size

    def project(self, stage_xy: Point2d) -> CopperProjection:
        """指定ステージ位置で視野内に想定される銅箔を投影する.

        Args:
            stage_xy: ステージのXY位置（機械座標、mm）

        Returns:
            塗り潰しマスクとエッジマスクの組
        """
        matrix, shift = self._board_to_pixel_affine(stage_xy)
        width, height = self._image_size
        fill_mask = np.zeros((height, width), dtype=np.uint8)
        edge_mask = np.zeros((height, width), dtype=np.uint8)

        view_bounds = self._view_bounds(matrix, shift)
        for polygon in self._polygons:
            if not _bounds_overlap(polygon.bounds, view_bounds):
                continue
            exterior = _ring_to_pixels(polygon.exterior.coords, matrix, shift)
            interiors = [
                _ring_to_pixels(ring.coords, matrix, shift)
                for ring in polygon.interiors
            ]

            # 穴の中に別の銅箔島が入れ子になり得るため、polygonごとに
            # exterior→255 / interiors→0 を描いてから合成する
            single = np.zeros_like(fill_mask)
            cv2.fillPoly(single, [exterior], 255)
            if interiors:
                cv2.fillPoly(single, interiors, 0)
            np.maximum(fill_mask, single, out=fill_mask)

            # フレーム端のクリップ線が偽エッジにならないよう、fillの輪郭では
            # なくpolygon境界そのものを描画する
            cv2.polylines(
                edge_mask, [exterior, *interiors], isClosed=True, color=255, thickness=1
            )

        return CopperProjection(fill_mask=fill_mask, edge_mask=edge_mask)

    def pixel_of(self, board_point: Point2d, stage_xy: Point2d) -> Point2d:
        """board座標の点を投影公式でpixel座標へ変換する.

        Args:
            board_point: board座標の点（mm）
            stage_xy: ステージのXY位置（機械座標、mm）

        Returns:
            全画面pixel座標の点
        """
        offset_mm = self._offset_transform.apply(
            stage_xy - self._board_transform.apply(board_point)
        )
        width, height = self._image_size
        return Point2d(
            x=width / 2 + self._pixel_per_mm * offset_mm.x,
            y=height / 2 + self._pixel_per_mm * offset_mm.y,
        )

    def roi_of(
        self,
        polygon: Polygon | Sequence[Polygon],
        stage_xy: Point2d,
        margin_mm: float = 1.0,
        min_size_mm: float = 3.0,
    ) -> PixelRect:
        """polygonの投影bboxにマージンを加えたROI矩形を返す.

        exterior全頂点を投影してbboxを取り、margin_mmを加える。複数
        ポリゴンを渡した場合は全体を覆うbboxを取る。min_size_mm未満の
        辺は中心対称に拡張し、フレームへクランプする。

        Args:
            polygon: 対象ポリゴン（shapely、mm単位、board座標）。複数可
            stage_xy: ステージのXY位置（機械座標、mm）
            margin_mm: bboxへ加えるマージン（mm）
            min_size_mm: ROIの最小辺長（mm）

        Returns:
            ROI矩形 (x0, y0, x1, y1)。半開区間、全画面pixel座標
        """
        polygons = [polygon] if isinstance(polygon, Polygon) else list(polygon)
        pixels = [
            self.pixel_of(Point2d(x=float(coord[0]), y=float(coord[1])), stage_xy)
            for poly in polygons
            for coord in poly.exterior.coords
        ]
        margin_px = margin_mm * self._pixel_per_mm
        x0 = min(p.x for p in pixels) - margin_px
        x1 = max(p.x for p in pixels) + margin_px
        y0 = min(p.y for p in pixels) - margin_px
        y1 = max(p.y for p in pixels) + margin_px

        min_px = min_size_mm * self._pixel_per_mm
        if x1 - x0 < min_px:
            center_x = (x0 + x1) / 2
            x0, x1 = center_x - min_px / 2, center_x + min_px / 2
        if y1 - y0 < min_px:
            center_y = (y0 + y1) / 2
            y0, y1 = center_y - min_px / 2, center_y + min_px / 2

        width, height = self._image_size
        return (
            max(0, math.floor(x0)),
            max(0, math.floor(y0)),
            min(width, math.ceil(x1)),
            min(height, math.ceil(y1)),
        )

    def _board_to_pixel_affine(
        self, stage_xy: Point2d
    ) -> tuple[ImageArray, ImageArray]:
        """Board座標→pixel座標のアフィン変換 (2x2行列, 平行移動) を導出する."""
        origin = self.pixel_of(Point2d(0.0, 0.0), stage_xy)
        unit_x = self.pixel_of(Point2d(1.0, 0.0), stage_xy)
        unit_y = self.pixel_of(Point2d(0.0, 1.0), stage_xy)
        matrix = np.array(
            [
                [unit_x.x - origin.x, unit_y.x - origin.x],
                [unit_x.y - origin.y, unit_y.y - origin.y],
            ]
        )
        shift = np.array([origin.x, origin.y])
        return matrix, shift

    def _view_bounds(self, matrix: ImageArray, shift: ImageArray) -> _Bounds:
        """pixel空間の4隅をboard座標へ逆変換し、視野のbboxを返す."""
        width, height = self._image_size
        corners_px = np.array(
            [[0.0, 0.0], [width, 0.0], [0.0, height], [width, height]]
        )
        corners_board = (corners_px - shift) @ np.linalg.inv(matrix).T
        return (
            float(corners_board[:, 0].min()),
            float(corners_board[:, 1].min()),
            float(corners_board[:, 0].max()),
            float(corners_board[:, 1].max()),
        )


class CopperEdgeMatcher:
    """観測エッジと想定エッジをchamfer距離で照合するクラス.

    推定するのは並進のみで、回転は扱わない（実運用のROIは数十px角しかなく、 微小回転は測定可能な信号にならないため）。並進は
    matchTemplate の 探索位置そのものなので整数pxになる。
    """

    def __init__(
        self,
        pixel_per_mm: float,
        search_window_mm: float = 2.0,
        crop_size: tuple[int, int] | None = None,
    ) -> None:
        """CopperEdgeMatcherを初期化する.

        Args:
            pixel_per_mm: pixel/mm比率
            search_window_mm: 探索窓の片側幅 (mm)
            crop_size: テンプレートとする中心領域サイズ (width, height)。
                Noneの場合はフレームから探索窓分を除いた最大領域
        """
        self._pixel_per_mm = pixel_per_mm
        self._window_px = round(search_window_mm * pixel_per_mm)
        self._crop_size = crop_size

    def match(
        self,
        observed_edges: ImageArray,
        expected_edges: ImageArray,
        roi: PixelRect | None = None,
    ) -> EdgeMatch | None:
        """観測エッジと想定エッジの並進ずれを照合する.

        ROI矩形からテンプレートを切り出し、観測エッジの距離変換（探索窓で
        キャップ）を滑らせて距離和が最小となる並進を求める。

        Args:
            observed_edges: 観測エッジマスク (uint8, 0/255)
            expected_edges: 想定エッジマスク (uint8, 0/255)。同サイズ
            roi: テンプレート矩形 (x0, y0, x1, y1)。Noneは中央クロップ

        Returns:
            照合結果（offset = 観測 − 想定）。観測エッジまたはROI内の
            想定エッジが空の場合はNone
        """
        if np.count_nonzero(observed_edges) == 0:
            return None

        height, width = observed_edges.shape[:2]
        x0, y0, x1, y1 = roi if roi is not None else self._template_rect(width, height)
        template = (expected_edges[y0:y1, x0:x1] > 0).astype(np.float32)
        if np.count_nonzero(template) == 0:
            return None

        window = self._window_px
        sx0 = max(0, x0 - window)
        sy0 = max(0, y0 - window)
        sx1 = min(width, x1 + window)
        sy1 = min(height, y1 + window)

        background = np.where(observed_edges > 0, 0, 255).astype(np.uint8)
        distance: ImageArray = cv2.distanceTransform(background, cv2.DIST_L2, 3)
        search = np.minimum(distance, float(window))[sy0:sy1, sx0:sx1].astype(
            np.float32
        )

        result: ImageArray = cv2.matchTemplate(search, template, cv2.TM_CCORR)
        min_val, _, min_loc, _ = cv2.minMaxLoc(result)

        # 探索領域はROIから窓分だけ外へ広げてあるので、その差を戻して
        # ROI基準の並進にする
        offset_px = Point2d(
            x=float(sx0 - x0 + min_loc[0]), y=float(sy0 - y0 + min_loc[1])
        )
        return EdgeMatch(
            offset=Offset(px=offset_px, pixel_per_mm=self._pixel_per_mm),
            mean_distance_px=float(min_val) / int(np.count_nonzero(template)),
        )

    def _template_rect(self, width: int, height: int) -> PixelRect:
        """テンプレート領域 (x0, y0, x1, y1) をフレーム内で決める."""
        if self._crop_size is None:
            crop_w = width - 2 * self._window_px
            crop_h = height - 2 * self._window_px
        else:
            crop_w, crop_h = self._crop_size
        crop_w = min(max(crop_w, 1), width)
        crop_h = min(max(crop_h, 1), height)
        x0 = (width - crop_w) // 2
        y0 = (height - crop_h) // 2
        return x0, y0, x0 + crop_w, y0 + crop_h
