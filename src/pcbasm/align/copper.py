"""設計銅箔の pixel 空間への投影と、観測エッジとの照合."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

import attrs
import cv2
import numpy as np
from shapely import Polygon
from shapely.coords import CoordinateSequence

from pcbasm.board import Layer
from pcbasm.geometry import Affine2d, Compose, Point2d, Transform
from pcbasm.vision import CameraModel, ImageArray, Offset, PixelRect

if TYPE_CHECKING:
    from pcbasm.align.board_frame import BoardFrame

type _Bounds = tuple[float, float, float, float]
_MIN_MATCH_SHARPNESS = 0.15


@attrs.frozen
class CopperProjection:
    """視野内の想定銅箔の pixel 空間表現.

    Attributes:
        fill_mask: 穴を反映した塗り潰しマスク (uint8, 0/255)。overlay 表示用
        edge_mask: polygon 境界の 1 px 線マスク (uint8, 0/255)。マッチング用
    """

    fill_mask: ImageArray = attrs.field(eq=False)
    edge_mask: ImageArray = attrs.field(eq=False)


@attrs.frozen
class EdgeMatch:
    """観測エッジと想定エッジの照合結果.

    Attributes:
        offset: サブ pixel 並進（観測 − 想定、px）
        rms_distance_px: 想定エッジ 1 点あたりの RMS chamfer 距離 [px]
    """

    offset: Offset
    rms_distance_px: float


def _ring_to_pixels(coords: CoordinateSequence, affine: Affine2d) -> ImageArray:
    """リング座標列 (mm) を pixel 座標の int32 配列へ変換する."""
    pixels = affine.map(np.asarray(coords, dtype=np.float64))
    return np.round(pixels).astype(np.int32).reshape(-1, 1, 2)


def _bounds_overlap(a: _Bounds, b: _Bounds) -> bool:
    """2 つの bbox (minx, miny, maxx, maxy) が重なるかを判定する."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


class CopperProjector:
    """設計銅箔ポリゴンをカメラ pixel 空間へ投影するクラス.

    写像は :meth:`CameraModel.machine_to_pixel` に board 変換を前置したもの。
    """

    def __init__(
        self,
        polygons: Sequence[Polygon],
        board_transform: Transform,
        camera_model: CameraModel,
    ) -> None:
        """CopperProjector を初期化する.

        Args:
            polygons: 銅箔ポリゴン列（shapely、mm 単位、board 座標）
            board_transform: board 座標→機械座標の変換
            camera_model: カメラの写像（出力マスクの大きさは ``camera_model.image_size``）
        """
        self._polygons = list(polygons)
        self._board_transform = board_transform
        self._camera_model = camera_model

    @classmethod
    def from_board_frame(
        cls, board_frame: BoardFrame, *, layer: Layer = Layer.TOP
    ) -> CopperProjector:
        """基板の位置関係から、対象レイヤの銅箔投影器を組む.

        外形だけの銅板のように ``layer`` の銅箔島が無い PCB では、投影対象が空になる。
        その場合も ``board_to_pixel``（変換とカメラだけに依存）は使える。
        """
        return cls(
            polygons=[
                copper.polygon
                for copper in board_frame.board.copper
                if copper.layer == layer
            ],
            board_transform=board_frame.board_transform,
            camera_model=board_frame.camera_model,
        )

    @property
    def polygons(self) -> tuple[Polygon, ...]:
        """投影対象の銅箔ポリゴン."""
        return tuple(self._polygons)

    def with_correction(self, machine_transform: Transform) -> CopperProjector:
        """機械座標の補正を board 変換の後段へ挿入した投影器を返す."""
        return CopperProjector(
            polygons=self._polygons,
            board_transform=Compose([self._board_transform, machine_transform]),
            camera_model=self._camera_model,
        )

    def project(self, stage_xy: Point2d) -> CopperProjection:
        """指定ステージ位置で視野内に想定される銅箔を投影する.

        Args:
            stage_xy: ステージの XY 位置（機械座標、mm）

        Returns:
            塗り潰しマスクとエッジマスクの組
        """
        affine = self.board_to_pixel(stage_xy)
        width, height = self._camera_model.image_size
        fill_mask = np.zeros((height, width), dtype=np.uint8)
        edge_mask = np.zeros((height, width), dtype=np.uint8)

        view_bounds = self._view_bounds(affine)
        for polygon in self._polygons:
            if not _bounds_overlap(polygon.bounds, view_bounds):
                continue
            exterior = _ring_to_pixels(polygon.exterior.coords, affine)
            interiors = [
                _ring_to_pixels(ring.coords, affine) for ring in polygon.interiors
            ]

            # 穴の中に別の銅箔島が入れ子になり得るため、polygon ごとに
            # exterior→255 / interiors→0 を描いてから合成する
            single = np.zeros_like(fill_mask)
            cv2.fillPoly(single, [exterior], 255)
            if interiors:
                cv2.fillPoly(single, interiors, 0)
            np.maximum(fill_mask, single, out=fill_mask)

            # フレーム端のクリップ線が偽エッジにならないよう、fill の輪郭ではなく
            # polygon 境界そのものを描画する
            cv2.polylines(
                edge_mask, [exterior, *interiors], isClosed=True, color=255, thickness=1
            )

        return CopperProjection(fill_mask=fill_mask, edge_mask=edge_mask)

    def pixel_of(self, board_point: Point2d, stage_xy: Point2d) -> Point2d:
        """Board 座標の点を全画面 pixel 座標へ写す（ステージは ``stage_xy`` [機械座標 mm]）."""
        return self._camera_model.machine_to_pixel(stage_xy).apply(
            self._board_transform.apply(board_point)
        )

    def board_to_pixel(self, stage_xy: Point2d) -> Affine2d:
        """Board 座標→pixel 座標のアフィン変換を返す."""
        return self._camera_model.board_to_pixel(self._board_transform, stage_xy)

    def _view_bounds(self, affine: Affine2d) -> _Bounds:
        """Pixel 空間の 4 隅を board 座標へ逆変換し、視野の bbox を返す."""
        width, height = self._camera_model.image_size
        corners_px = np.array(
            [[0.0, 0.0], [width, 0.0], [0.0, height], [width, height]]
        )
        corners_board = affine.inverse().map(corners_px)
        return (
            float(corners_board[:, 0].min()),
            float(corners_board[:, 1].min()),
            float(corners_board[:, 0].max()),
            float(corners_board[:, 1].max()),
        )


def _fit_sharpness(patch: ImageArray, template_count: int) -> float:
    """3x3 コスト近傍の弱軸曲率を RMS 距離尺度で返す."""
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
    """3x3 コスト近傍からサブ pixel 位置と補間最小コストを返す."""
    cxm, c0, cxp = float(patch[1, 0]), float(patch[1, 1]), float(patch[1, 2])
    cym, cyp = float(patch[0, 1]), float(patch[2, 1])
    denx = cxm - 2 * c0 + cxp
    deny = cym - 2 * c0 + cyp
    dsx = (cxm - cxp) / (2 * denx) if denx > 0 else 0.0
    dsy = (cym - cyp) / (2 * deny) if deny > 0 else 0.0
    cstar = c0 - 0.25 * ((cxm - cxp) * dsx + (cym - cyp) * dsy)
    return dsx, dsy, cstar


class CopperEdgeMatcher:
    """観測エッジと想定エッジを chamfer 距離で照合するクラス.

    推定するのは並進のみで、回転は扱わない。最小コスト近傍を放物線補間して サブ pixel 変位を求め、弱軸の曲率が無い縮退 match
    は棄却する。
    """

    def __init__(
        self,
        pixel_per_mm: float,
        search_window_mm: float = 2.0,
    ) -> None:
        """CopperEdgeMatcher を初期化する.

        Args:
            pixel_per_mm: pixel/mm 比率
            search_window_mm: 探索窓の片側幅 (mm)
        """
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

        ROI 矩形からテンプレートを切り出し、観測エッジの距離変換（探索窓で
        キャップ）を滑らせて距離和が最小となる並進を求める。

        Args:
            observed_edges: 観測エッジマスク (uint8, 0/255)
            expected_edges: 想定エッジマスク (uint8, 0/255)。同サイズ
            roi: テンプレート矩形 (x0, y0, x1, y1)

        Returns:
            照合結果（offset = 観測 − 想定）。次の場合は None を返す。

            - 観測エッジ、または ROI 内の想定エッジが空
            - 最小コスト位置が探索窓の端にある（ずれが窓を超えた可能性）
            - コスト最小が鋭くない（直線 1 本だけなど、ずれが一意に決まらない）
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

        # 探索領域は ROI から窓の分だけ外へ広げてあるので、その差を戻して
        # ROI 基準の並進にする
        offset_px = Point2d(x=float(sx0 - x0 + cx + dsx), y=float(sy0 - y0 + cy + dsy))
        return EdgeMatch(
            offset=Offset(px=offset_px),
            rms_distance_px=math.sqrt(max(cstar, 0.0) / count),
        )
