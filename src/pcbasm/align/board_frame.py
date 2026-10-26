"""基板の位置関係（カメラの回転と board → 機械の変換）を計測する."""

import logging
from collections.abc import Callable
from enum import Enum, auto

import attrs
import numpy as np

from pcbasm.align.camera_rotation import CameraRotationMeasurer
from pcbasm.align.observe import (
    DETECTION_MAX_ATTEMPTS,
    DETECTION_RETRY_SEC,
    OffsetObserver,
)
from pcbasm.align.position import XYPositionAdjustor
from pcbasm.board import Board, Outline
from pcbasm.config import MachineConfig
from pcbasm.config.reference_point import CornerOffsetsConfig, ReferencePointConfig
from pcbasm.gcode import GCode
from pcbasm.geometry import (
    Compose,
    Matrix2d,
    Point2d,
    Shift,
    Transform,
)
from pcbasm.hal import Camera, KlipperClient, Speed, XYZStage
from pcbasm.vision import (
    CameraModel,
    FrameSink,
    reference_marker_detector,
    safe_move_distance,
)

logger = logging.getLogger(__name__)


class Corner(Enum):
    """ボードのコーナーを表す列挙型."""

    TOP_LEFT = auto()
    TOP_RIGHT = auto()
    BOTTOM_LEFT = auto()
    BOTTOM_RIGHT = auto()


def corner_offset(offsets: CornerOffsetsConfig, corner: Corner) -> Point2d:
    """指定コーナーのボード端から基準点マーカーへのオフセットを Point2d で返す."""
    match corner:
        case Corner.TOP_LEFT:
            offset = offsets.top_left
        case Corner.TOP_RIGHT:
            offset = offsets.top_right
        case Corner.BOTTOM_LEFT:
            offset = offsets.bottom_left
        case Corner.BOTTOM_RIGHT:
            offset = offsets.bottom_right

    return Point2d(x=offset[0], y=offset[1])


def _board_corner(outline: Outline, corner: Corner) -> Point2d:
    """指定コーナーの board 座標."""
    match corner:
        case Corner.TOP_LEFT:
            return Point2d(0.0, 0.0)
        case Corner.TOP_RIGHT:
            return Point2d(outline.width, 0.0)
        case Corner.BOTTOM_LEFT:
            return Point2d(0.0, outline.height)
        case Corner.BOTTOM_RIGHT:
            return Point2d(outline.width, outline.height)


def marker_position(
    outline: Outline, offsets: CornerOffsetsConfig, corner: Corner
) -> Point2d:
    """指定コーナーの基準点マーカーの board 座標."""
    return _board_corner(outline, corner) + corner_offset(offsets, corner)


class BoardTransformMeasurer:
    """Board 座標から機械座標への変換を計測するクラス.

    4 つの reference point の実測位置から affine 変換を最小二乗推定する。

    Example:
        from pcbasm.kicad import load_board

        board = load_board(pcb_file)
        measurer = BoardTransformMeasurer(
            adjust_reference=adjust_reference,
            klipper=klipper,
            stage=stage,
            outline=board.outline,
            reference_point=machine.reference_point,
            settle_sec=machine.settle.move_sec,
        )
        transform = measurer.measure()
        machine_pos = transform.apply(board_pos)
    """

    def __init__(
        self,
        adjust_reference: Callable[[], Point2d],
        klipper: KlipperClient,
        stage: XYZStage,
        outline: Outline,
        reference_point: ReferencePointConfig,
        move_velocity_ratio: float = 0.9,
        *,
        settle_sec: float,
    ) -> None:
        """BoardTransformMeasurer を初期化する.

        Args:
            adjust_reference: 基準点マーカーを画像中心に合わせた機械座標を返す関数
                （通常は ``XYPositionAdjustor.adjust``）
            klipper: Klipper クライアント
            stage: XYZ ステージ
            outline: Board Outline（width/height の取得に使用）
            reference_point: 基準点設定
            move_velocity_ratio: 最大速度に対する移動速度の割合 (0.0-1.0)
            settle_sec: 移動後の静定待ち [sec]（``machine.settle.move_sec``）
        """
        self._adjust_reference = adjust_reference
        self._klipper = klipper
        self._stage = stage
        self._outline = outline
        self._ref_point = reference_point
        self._move_velocity_ratio = move_velocity_ratio
        self._settle_sec = settle_sec

    def measure(self) -> Compose:
        """4 点の最小二乗法で board→機械座標の変換を計測する.

        TOP_LEFT → TOP_RIGHT → BOTTOM_RIGHT → BOTTOM_LEFT の順に概略位置へ移動する。

        各コーナーでは ``adjust_reference()`` が返した位置を実測点にする。

        Returns:
            Board 座標→機械座標の Compose 変換 (Matrix2d → Shift)

        Raises:
            ValueError: 基準点配置の design matrix の rank が 3 未満の場合
            RuntimeError: ``adjust_reference`` の失敗をそのまま送出する
        """
        logger.info("Board変換の計測を開始")

        corners = (
            Corner.TOP_LEFT,
            Corner.TOP_RIGHT,
            Corner.BOTTOM_RIGHT,
            Corner.BOTTOM_LEFT,
        )
        logger.info("計測コーナー: TOP_LEFT, TOP_RIGHT, BOTTOM_RIGHT, BOTTOM_LEFT")
        move_velocity = self._stage.max_velocity * self._move_velocity_ratio
        machine_points = [
            self._measure_corner(corner, move_velocity) for corner in corners
        ]

        board_points = tuple(
            marker_position(self._outline, self._ref_point.offsets, corner)
            for corner in corners
        )
        design = np.array(
            [[point.x, point.y, 1.0] for point in board_points],
        )
        measured = np.array([[point.x, point.y] for point in machine_points])
        params, _, rank, _ = np.linalg.lstsq(design, measured, rcond=None)
        if rank < 3:
            raise ValueError(
                f"基準点配置のdesign matrixのrankが不足しています: rank={rank}"
            )

        matrix_values = params[:2].T
        translation = params[2]
        matrix = Matrix2d(matrix_values)
        shift = Shift(x=float(translation[0]), y=float(translation[1]))
        logger.info(f"変換行列:\n{matrix_values}")
        logger.info(f"Board原点の機械座標: ({shift.x:.4f}, {shift.y:.4f})")

        # 変換を構成（Matrix2d → Shift）
        transform = Compose([matrix, shift])
        for corner, board_point, machine_point in zip(
            corners, board_points, machine_points, strict=True
        ):
            residual = transform.apply(board_point) - machine_point
            logger.info(
                f"残差 {corner.name}: ({residual.x:.4f}, {residual.y:.4f}) mm, "
                f"距離 {residual.norm:.4f} mm"
            )

        logger.info("Board変換の計測完了")
        return transform

    def _measure_corner(
        self,
        corner: Corner,
        move_velocity: float,
    ) -> Point2d:
        """指定コーナーへ移動し、位置補正した座標を返す."""
        ref_pos = self._approximate_position(corner)

        logger.info(f"=== {corner.name} Reference Pointへ移動 ===")
        logger.info(f"目標位置: ({ref_pos.x:.3f}, {ref_pos.y:.3f})")
        self._klipper.send_gcode(
            self._stage.move_settled(
                x=ref_pos.x,
                y=ref_pos.y,
                speed=Speed.absolute(move_velocity),
                settle_sec=self._settle_sec,
            ),
        )

        logger.info(f"=== {corner.name} Reference Pointの位置補正 ===")
        pos = self._adjust_reference()
        logger.info(f"{corner.name}位置: ({pos.x:.4f}, {pos.y:.4f})")
        return pos

    def _approximate_position(self, corner: Corner) -> Point2d:
        """指定コーナーの基準点マーカーへ移動するための概略マシン座標を返す.

        ボード左上コーナーの概略位置 = 左上基準点の概略位置 - 左上のオフセット。
        各コーナーの概略位置 = ボード左上コーナーの概略位置 + コーナーの board 座標 + そのコーナーのオフセット。
        """
        ref = self._ref_point
        top_left = Point2d(ref.x, ref.y)
        if corner == Corner.TOP_LEFT:
            return top_left
        board_origin = top_left - corner_offset(ref.offsets, Corner.TOP_LEFT)
        return (
            board_origin
            + _board_corner(self._outline, corner)
            + corner_offset(ref.offsets, corner)
        )


@attrs.frozen
class BoardFrame:
    """計測した基板の位置関係（値）.

    装置（Klipper・ステージ・カメラ）は持たない。使う側が別に受け取る。
    camera_model と board_transform の意味は :mod:`pcbasm.align` の冒頭を参照。
    """

    board: Board
    camera_model: CameraModel
    board_transform: Transform


def measure_board_frame(
    board: Board,
    machine: MachineConfig,
    *,
    klipper: KlipperClient,
    stage: XYZStage,
    camera: Camera,
    frame_sink: FrameSink | None = None,
) -> BoardFrame:
    """ホーミングから Board 変換の計測までを行い、基板の位置関係を返す.

    手順はカメラ準備 → キャリブレーション読込 → ホーミング (G28) → 左上基準点へ移動 → カメラ回転角の計測 → Board 変換の計測。

    この関数自体は終了時の駐機をしない（ジョブ管理が行う）。

    Args:
        board: 計測する基板
        machine: マシン設定
        klipper: Klipper への接続（塗布の本体と同じ timeout 無し）
        stage: klipper のステージ
        camera: 使用するカメラ
        frame_sink: 検出注釈画像を送る sink。None の場合は表示しない

    Raises:
        CircleDetectionError: 基準点マーカーを規定回数の再取得でも検出できない場合
        RuntimeError: 位置補正が収束しない場合、または G-code 送信が失敗した場合
        ValueError: 基準点配置から Board 変換を決められない場合
    """
    outline = board.outline
    logger.info("Board幅: %.3f mm", outline.width)
    logger.info("Board高さ: %.3f mm", outline.height)

    # カメラ初期化
    logger.info("=== カメラ初期化 ===")
    cam_config = machine.camera
    logger.info("カメラ: %s", camera.info.name)
    logger.info("解像度: %sx%s", cam_config.width, cam_config.height)

    # キャリブレーション結果読み込み
    logger.info("=== キャリブレーション読み込み ===")
    camera_model = CameraModel.load(cam_config.calibration_file)
    logger.info("pixel/mm: %.2f", camera_model.pixel_per_mm)

    # 円検出器初期化
    ref_config = machine.reference_point
    detector = reference_marker_detector(
        camera_model,
        cam_config.crop.size,
        target_diameter_mm=ref_config.target_diameter,
        diameter_tolerance_mm=ref_config.diameter_tolerance_mm,
    )

    # ホーミング
    logger.info("=== ホーミング (G28) ===")
    klipper.send_gcode(GCode.homing(x=True, y=True, z=True) + GCode.wait_for_done())
    logger.info("ホーミング完了")

    # Reference Point へ移動
    logger.info("=== Reference Point (top left) へ移動 ===")
    logger.info("目標位置: (%s, %s)", ref_config.x, ref_config.y)
    logger.info("キャリブレーションZ位置: %.3f mm", camera_model.focus_z)
    settle = machine.settle
    klipper.send_gcode(
        stage.move_settled(
            x=ref_config.x,
            y=ref_config.y,
            z=camera_model.focus_z,
            settle_sec=settle.move_sec,
        )
    )
    logger.info("移動完了")

    # オフセット検出関数を定義
    observer = OffsetObserver(
        detector=detector,
        camera=camera,
        camera_model=camera_model,
        crop_size=cam_config.crop.size,
        frame_sink=frame_sink,
        sample_count=machine.detection.sample_count,
        minimum_sample_count=machine.detection.minimum_sample_count,
        # 有効検出数の下限で判定するようになったため、1 バッチ失敗しただけで
        # ジョブの最初のステップが止まらないよう撮り直す（塗布痕側と同じ方針）
        max_attempts=DETECTION_MAX_ATTEMPTS,
        retry_sec=DETECTION_RETRY_SEC,
    )

    # カメラ回転角の計測（2 点法）
    logger.info("=== カメラ回転角の計測 ===")
    move_distance = (
        safe_move_distance(cam_config.crop.size, margin=0.3) / camera_model.pixel_per_mm
    )
    camera_rotation_measurer = CameraRotationMeasurer(
        observe=observer.observe,
        klipper=klipper,
        stage=stage,
        move_distance=move_distance,
        settle_sec=settle.move_sec,
    )
    camera_model = camera_model.with_rotation(camera_rotation_measurer.measure())

    # 位置補正を行い最終座標を返す関数を定義
    position_adjustor = XYPositionAdjustor(
        observe=observer.observe,
        klipper=klipper,
        stage=stage,
        camera_model=camera_model,
        tolerance=machine.alignment.tolerance,
        settle_sec=settle.move_sec,
    )

    # Board 変換の計測
    logger.info("=== Board変換の計測 ===")
    board_transform_measurer = BoardTransformMeasurer(
        adjust_reference=position_adjustor.adjust,
        klipper=klipper,
        stage=stage,
        outline=outline,
        reference_point=ref_config,
        settle_sec=settle.move_sec,
    )
    board_transform = board_transform_measurer.measure()

    return BoardFrame(
        board=board, camera_model=camera_model, board_transform=board_transform
    )
