"""Board座標から機械座標への変換を計測する."""

import logging
import math
from collections.abc import Sequence

import numpy as np

from pcbasm import gcode
from pcbasm.config import BoardAlign, Corner, ReferencePoint
from pcbasm.geometry import Compose, Matrix2d, Point2d, Shift, Transform
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.pcb import Outline
from pcbasm.posctrl.copper import CopperEdgeMatcher, CopperProjector, PixelRect
from pcbasm.posctrl.orthogonality import OrthogonalityMetrics
from pcbasm.posctrl.pad import CopperPadObserver
from pcbasm.posctrl.position import XYPositionAdjustor
from pcbasm.utils import get_class_module_path
from pcbasm.vision import CopperEdgeDetector, FrameSink

# 矩形順（時計回り）のコーナー巡回順
_RECT_ORDER = (
    Corner.TOP_LEFT,
    Corner.TOP_RIGHT,
    Corner.BOTTOM_RIGHT,
    Corner.BOTTOM_LEFT,
)


def _corner_order(anchor: Corner) -> tuple[Corner, ...]:
    """アンカーを先頭にした矩形順のコーナー巡回順を返す."""
    start = _RECT_ORDER.index(anchor)
    return _RECT_ORDER[start:] + _RECT_ORDER[:start]


def fit_affine_transform(
    board_points: Sequence[Point2d], machine_points: Sequence[Point2d]
) -> Compose:
    """対応点列の最小二乗フィットでboard→機械座標のアフィン変換を解く.

    m ≈ M b + t を design=(n,3)[bx,by,1], target=(n,2)[mx,my] の
    np.linalg.lstsq で解き、M=params[:2].T, t=params[2] を得る。

    Args:
        board_points: board座標の対応点列 (mm)
        machine_points: 機械座標の対応点列 (mm)。board_pointsと同数

    Returns:
        Board座標→機械座標のCompose変換 (Matrix2d → Shift)

    Raises:
        ValueError: 対応点数が一致しない、または3点未満の場合
    """
    if len(board_points) != len(machine_points):
        raise ValueError(
            "対応点数が一致しません: "
            f"board={len(board_points)}, machine={len(machine_points)}"
        )
    if len(board_points) < 3:
        raise ValueError(
            f"アフィンフィットには3点以上の対応点が必要です: {len(board_points)}点"
        )
    design = np.array([[b.x, b.y, 1.0] for b in board_points])
    target = np.array([[m.x, m.y] for m in machine_points])
    params, *_ = np.linalg.lstsq(design, target, rcond=None)
    matrix = Matrix2d(params[:2].T)
    shift = Shift(x=float(params[2][0]), y=float(params[2][1]))
    return Compose([matrix, shift])


class BoardTransformMeasurer:
    """基板4隅の輪郭照合でboard座標→機械座標の変換を計測するクラス.

    基準点マーカーへのサーボ収束位置から並進のみの初期変換 T0 を推定し、
    T0 で基板4隅へカメラを運び、設計外形の投影と観測エッジの照合サーボで
    各コーナーの機械座標を実測、4対応点の最小二乗フィットで並進・回転・
    スケールを一括推定する。

    コーナーは外形ポリゴンbboxの4隅（左上原点の矩形規約）。bboxコーナー
    ±edge_length に外形ジオメトリが無い基板は照合できない。

    Example:
        measurer = BoardTransformMeasurer(
            camera=camera,
            klipper=klipper,
            stage=stage,
            outline=pcb.outline,
            reference_point=machine.reference_point,
            offset_transform=offset_transform,
            pixel_per_mm=calibration.pixel_per_mm,
            image_size=calibration.resolution,
            board_align=machine.board_align,
        )
        transform = measurer.measure(marker_pos)
        machine_pos = transform.apply(board_pos)
    """

    def __init__(
        self,
        *,
        camera: Camera,
        klipper: Klipper,
        stage: XYZStage,
        outline: Outline,
        reference_point: ReferencePoint,
        offset_transform: Transform,
        pixel_per_mm: float,
        image_size: tuple[int, int],
        board_align: BoardAlign,
        settle_time: float = 0.5,
        frame_sink: FrameSink | None = None,
    ) -> None:
        """BoardTransformMeasurerを初期化する.

        Args:
            camera: カメラ
            klipper: Klipperクライアント
            stage: XYZステージ
            outline: Board Outline（外形ポリゴンとwidth/heightの取得に使用）
            reference_point: 基準点設定（アンカーコーナーとオフセット）
            offset_transform: 観測オフセット系から機械座標系への変換
            pixel_per_mm: pixel/mm比率
            image_size: カメラフレームのサイズ (width, height)
            board_align: 基板コーナー照合の設定
            settle_time: 移動後の安定待機時間（秒）
            frame_sink: 観測ごとに照合状況フレームを送る sink。
                Noneの場合は送らない
        """
        self._camera = camera
        self._klipper = klipper
        self._stage = stage
        self._outline = outline
        self._ref_point = reference_point
        self._offset_transform = offset_transform
        self._pixel_per_mm = pixel_per_mm
        self._image_size = image_size
        self._board_align = board_align
        self._settle_time = settle_time
        self._frame_sink = frame_sink

        self._edge_detector = CopperEdgeDetector(
            canny_low=board_align.canny_low,
            canny_high=board_align.canny_high,
            blur_ksize=board_align.blur_ksize,
        )
        self._matcher = CopperEdgeMatcher(
            pixel_per_mm=pixel_per_mm,
            search_window_mm=board_align.search_window,
            theta_range_degrees=board_align.theta_range,
        )
        self._roi = self._corner_roi()

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def measure(self, marker_pos: Point2d) -> Compose:
        """基板4隅の輪郭照合でboard→機械座標の変換を計測する.

        Args:
            marker_pos: 基準点マーカーへのサーボ収束位置（機械座標、mm）

        Returns:
            Board座標→機械座標のCompose変換 (Matrix2d → Shift)

        Raises:
            RuntimeError: いずれかのコーナーで照合・収束に失敗した場合
        """
        self._logger.info("Board変換の計測を開始")
        width = self._outline.width
        height = self._outline.height
        anchor = self._ref_point.corner

        # 並進のみ・恒等回転の初期変換 T0（marker = T0(corner) + offset）
        t0 = Shift.from_point(
            marker_pos
            - self._ref_point.offset_point()
            - anchor.board_position(width, height)
        )
        projector = CopperProjector(
            polygons=[self._outline.polygon],
            board_transform=t0,
            offset_transform=self._offset_transform,
            pixel_per_mm=self._pixel_per_mm,
            image_size=self._image_size,
        )

        corners = _corner_order(anchor)
        board_points = [corner.board_position(width, height) for corner in corners]
        machine_points = [
            self._measure_corner(corner, t0.apply(board_point), projector)
            for corner, board_point in zip(corners, board_points, strict=True)
        ]

        transform = fit_affine_transform(board_points, machine_points)
        for corner, board_point, machine_point in zip(
            corners, board_points, machine_points, strict=True
        ):
            residual = transform.apply(board_point) - machine_point
            self._logger.info(
                "残差 %s: (%.4f, %.4f) mm, 距離 %.4f mm",
                corner.value,
                residual.x,
                residual.y,
                residual.norm,
            )
        metrics = OrthogonalityMetrics.from_transform(transform)
        self._logger.info(
            "直交性: 軸間角ずれ %+.3f deg / scale X %.5f Y %.5f",
            metrics.axis_angle_error_deg,
            metrics.scale_x,
            metrics.scale_y,
        )

        self._logger.info("Board変換の計測完了")
        return transform

    def _measure_corner(
        self, corner: Corner, anchor: Point2d, projector: CopperProjector
    ) -> Point2d:
        """指令位置anchorへ移動し、輪郭照合サーボの収束位置を返す.

        Raises:
            RuntimeError: 照合不能・誤マッチ棄却・非収束の場合
        """
        self._logger.info(
            "=== コーナー %s の照合: 指令位置 (%.3f, %.3f) ===",
            corner.value,
            anchor.x,
            anchor.y,
        )
        self._klipper.send_gcode(
            self._stage.move(x=anchor.x, y=anchor.y, speed=Speed.rate(0.5))
            + gcode.wait(self._settle_time)
            + gcode.wait_for_done()
        )

        # 投影アンカーは指令位置に固定する（サーボ中は再投影しない）
        projection = projector.project(anchor)
        observer = CopperPadObserver(
            camera=self._camera,
            edge_detector=self._edge_detector,
            matcher=self._matcher,
            projection=projection,
            roi=self._roi,
            frame_sink=self._frame_sink,
            max_offset_mm=self._board_align.max_correction,
        )
        adjustor = XYPositionAdjustor(
            observe=observer.observe,
            klipper=self._klipper,
            stage=self._stage,
            offset_transform=self._offset_transform,
            tolerance=self._board_align.tolerance,
            settle_time=self._settle_time,
        )
        try:
            position = adjustor.adjust()
        except RuntimeError as exc:
            raise RuntimeError(
                f"コーナー {corner.value} の輪郭照合に失敗しました: {exc}"
                "（基板の固定・クランプ位置・[board_align] のCanny閾値を"
                "確認してください）"
            ) from exc
        self._logger.info(
            "%s 収束位置: (%.4f, %.4f)", corner.value, position.x, position.y
        )
        return position

    def _corner_roi(self) -> PixelRect:
        """画像中心の固定正方形ROIを返す（片側 edge_length·ppm、フレームにクランプ）."""
        width, height = self._image_size
        half = self._board_align.edge_length * self._pixel_per_mm
        return (
            max(0, math.floor(width / 2 - half)),
            max(0, math.floor(height / 2 - half)),
            min(width, math.ceil(width / 2 + half)),
            min(height, math.ceil(height / 2 + half)),
        )
