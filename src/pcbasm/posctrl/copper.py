"""設計銅箔のpixel空間への投影と、観測エッジとの照合."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

import attrs
import cv2
import numpy as np
from shapely import Polygon
from shapely.coords import CoordinateSequence

from pcbasm.geometry import Compose, Point2d, Shift, Transform
from pcbasm.pcb import Layer
from pcbasm.vision import ImageArray, Offset

if TYPE_CHECKING:
    from pcbasm.posctrl.setup import BoardCalibrationResult

type _Bounds = tuple[float, float, float, float]
# ROI矩形 (x0, y0, x1, y1)。半開区間、全画面pixel座標
type PixelRect = tuple[int, int, int, int]
_MIN_MATCH_SHARPNESS = 0.15


def centered_roi(image_size: tuple[int, int], size_px: int) -> PixelRect:
    """画像中心に一辺 ``size_px`` の正方 ROI を返す."""
    if size_px < 1:
        raise ValueError(f"size_pxは1以上である必要があります: {size_px}")
    width, height = image_size
    if size_px > min(image_size):
        raise ValueError(f"size_px {size_px} が画像サイズ {image_size} を超えています")
    x0 = (width - size_px) // 2
    y0 = (height - size_px) // 2
    return (x0, y0, x0 + size_px, y0 + size_px)


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
        offset: サブpixel並進（観測 − 想定、px。``.mm`` でmm）
        rms_distance_px: 想定エッジ1点あたりのRMS chamfer距離 [px]
    """

    offset: Offset
    rms_distance_px: float

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

    @classmethod
    def from_calibration(
        cls, result: BoardCalibrationResult, *, layer: Layer = Layer.TOP
    ) -> CopperProjector:
        """Board 計測結果の変換・calibration から、対象レイヤの銅箔投影器を組む.

        外形だけの銅板のように ``layer`` の銅箔島が無い PCB では、投影対象が空でも
        ``board_to_pixel_affine``（変換と calibration だけに依存）は使える。
        """
        return cls(
            polygons=[
                copper.polygon for copper in result.pcb.copper if copper.layer == layer
            ],
            board_transform=result.board_transform,
            offset_transform=result.offset_transform,
            pixel_per_mm=result.calibration.pixel_per_mm,
            image_size=result.calibration.resolution,
        )

    @property
    def polygons(self) -> tuple[Polygon, ...]:
        """投影対象の銅箔ポリゴン."""
        return tuple(self._polygons)

    def with_correction(self, machine_transform: Transform) -> CopperProjector:
        """機械座標の補正を board 変換の後段へ挿した投影器を返す."""
        return CopperProjector(
            polygons=self._polygons,
            board_transform=Compose([self._board_transform, machine_transform]),
            offset_transform=self._offset_transform,
            pixel_per_mm=self._pixel_per_mm,
            image_size=self._image_size,
        )

    def project(self, stage_xy: Point2d) -> CopperProjection:
        """指定ステージ位置で視野内に想定される銅箔を投影する.

        Args:
            stage_xy: ステージのXY位置（機械座標、mm）

        Returns:
            塗り潰しマスクとエッジマスクの組
        """
        matrix, shift = self.board_to_pixel_affine(stage_xy)
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

    def board_to_pixel_affine(self, stage_xy: Point2d) -> tuple[ImageArray, ImageArray]:
        """Board座標→pixel座標のアフィン変換を返す."""
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


def _fit_sharpness(patch: ImageArray, template_count: int) -> float:
    """3x3コスト近傍の弱軸曲率をRMS距離尺度で返す."""
    s0 = float(patch.sum())
    sx2 = float(patch[:, 0].sum() + patch[:, 2].sum())
    sy2 = float(patch[0, :].sum() + patch[2, :].sum())
    sxy = float(patch[0, 0] - patch[0, 2] - patch[2, 0] + patch[2, 2])
    a3 = -s0 / 3 + sx2 / 2
    a4 = -s0 / 3 + sy2 / 2
    a5 = sxy / 4
    lambda_min = a3 + a4 - math.hypot(a3 - a4, a5)
    return math.sqrt(max(lambda_min, 0.0) / template_count)


def _parabolic_subpixel(patch: ImageArray) -> tuple[float, float, float]:
    """3x3コスト近傍からサブpixel位置と補間最小コストを返す."""
    cxm, c0, cxp = float(patch[1, 0]), float(patch[1, 1]), float(patch[1, 2])
    cym, cyp = float(patch[0, 1]), float(patch[2, 1])
    denx = cxm - 2 * c0 + cxp
    deny = cym - 2 * c0 + cyp
    dsx = (cxm - cxp) / (2 * denx) if denx > 0 else 0.0
    dsy = (cym - cyp) / (2 * deny) if deny > 0 else 0.0
    cstar = c0 - 0.25 * ((cxm - cxp) * dsx + (cym - cyp) * dsy)
    return dsx, dsy, cstar


class CopperEdgeMatcher:
    """観測エッジと想定エッジをchamfer距離で照合するクラス.

    推定するのは並進のみで、回転は扱わない。最小コスト近傍を放物線補間して
    サブpixel変位を求め、弱軸の曲率が無い縮退matchは棄却する。
    """

    def __init__(
        self,
        pixel_per_mm: float,
        search_window_mm: float = 2.0,
    ) -> None:
        """CopperEdgeMatcherを初期化する.

        Args:
            pixel_per_mm: pixel/mm比率
            search_window_mm: 探索窓の片側幅 (mm)
        """
        self._pixel_per_mm = pixel_per_mm
        self._window_px = round(search_window_mm * pixel_per_mm)

    @property
    def window_px(self) -> int:
        """探索窓の片側幅 [px]."""
        return self._window_px

    def match(
        self,
        observed_edges: ImageArray,
        expected_edges: ImageArray,
        roi: PixelRect,
    ) -> EdgeMatch | None:
        """観測エッジと想定エッジの並進ずれを照合する.

        ROI矩形からテンプレートを切り出し、観測エッジの距離変換（探索窓で
        キャップ）を滑らせて距離和が最小となる並進を求める。

        Args:
            observed_edges: 観測エッジマスク (uint8, 0/255)
            expected_edges: 想定エッジマスク (uint8, 0/255)。同サイズ
            roi: テンプレート矩形 (x0, y0, x1, y1)

        Returns:
            照合結果（offset = 観測 − 想定）。次の場合はNone:

            - 観測エッジ、またはROI内の想定エッジが空
            - 最小コスト位置が探索窓の端にある（ずれが窓を超えた可能性）
            - コスト最小が鋭くない（直線1本だけなど、ずれが一意に決まらない）
        """
        if np.count_nonzero(observed_edges) == 0:
            return None

        height, width = observed_edges.shape[:2]
        x0, y0, x1, y1 = roi
        template = (expected_edges[y0:y1, x0:x1] > 0).astype(np.float32)
        count = int(np.count_nonzero(template))
        if count == 0:
            return None

        window = self._window_px
        sx0 = max(0, x0 - window)
        sy0 = max(0, y0 - window)
        sx1 = min(width, x1 + window)
        sy1 = min(height, y1 + window)

        background = np.where(observed_edges > 0, 0, 255).astype(np.uint8)
        distance: ImageArray = cv2.distanceTransform(
            background, cv2.DIST_L2, cv2.DIST_MASK_PRECISE
        )
        search = (np.minimum(distance, float(window)) ** 2)[sy0:sy1, sx0:sx1].astype(
            np.float32
        )

        result: ImageArray = cv2.matchTemplate(search, template, cv2.TM_CCORR)
        _, _, min_loc, _ = cv2.minMaxLoc(result)
        cx, cy = min_loc
        rows, cols = result.shape[:2]
        if not (0 < cx < cols - 1 and 0 < cy < rows - 1):
            return None

        patch = result[cy - 1 : cy + 2, cx - 1 : cx + 2].astype(np.float64)
        if _fit_sharpness(patch, count) < _MIN_MATCH_SHARPNESS:
            return None
        dsx, dsy, cstar = _parabolic_subpixel(patch)

        # 探索領域はROIから窓分だけ外へ広げてあるので、その差を戻して
        # ROI基準の並進にする
        offset_px = Point2d(x=float(sx0 - x0 + cx + dsx), y=float(sy0 - y0 + cy + dsy))
        return EdgeMatch(
            offset=Offset(px=offset_px, pixel_per_mm=self._pixel_per_mm),
            rms_distance_px=math.sqrt(max(cstar, 0.0) / count),
        )
