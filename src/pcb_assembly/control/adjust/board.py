"""Board座標から機械座標への変換を計測する."""

import logging
from collections.abc import Callable

from pcb_assembly import gcode
from pcb_assembly.config import Corner, ReferencePoint
from pcb_assembly.geometry import (
    Compose,
    Move,
    Point2d,
    Rotation,
    Trajectory,
    Translation,
)
from pcb_assembly.hal import Klipper, XYZStage
from pcb_assembly.pcb import Outline
from pcb_assembly.utils import get_class_module_path


class BoardTransformMeasurer:
    """Board座標から機械座標への変換を計測するクラス.

    2点法を用いて、left/right reference pointの実測位置から
    回転と平行移動を計算する。

    Example:
        from pcb_assembly.pcb.kicad import extract_outline

        outline = extract_outline(pcb_file)
        measurer = BoardTransformMeasurer(
            outline=outline,
            reference_point=machine.reference_point,
        )
        # adjust_reference: 補正済みオフセットを返す関数
        transform = measurer.measure(adjust_reference, klipper, stage)
        machine_pos = transform.apply(board_pos)
    """

    def __init__(
        self,
        outline: Outline,
        reference_point: ReferencePoint,
        move_velocity_ratio: float = 0.9,
        settle_time: float = 0.5,
    ) -> None:
        """BoardTransformMeasurerを初期化する.

        Args:
            outline: Board Outline（widthの取得に使用）
            reference_point: 基準点設定
            move_velocity_ratio: 最大速度に対する移動速度の割合 (0.0-1.0)
            settle_time: 移動後の安定待機時間（秒）
        """
        self._outline = outline
        self._ref_point = reference_point
        self._move_velocity_ratio = move_velocity_ratio
        self._settle_time = settle_time

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def measure(
        self,
        adjust_reference: Callable[[], Point2d],
        klipper: Klipper,
        stage: XYZStage,
    ) -> Compose:
        """2点法でboard→機械座標の変換を計測する.

        Args:
            adjust_reference: 補正済みオフセットを返す関数
            klipper: Klipperクライアント
            stage: XYZステージ

        Returns:
            Board座標→機械座標のCompose変換
        """
        self._logger.info("Board変換の計測を開始")

        # 事前計算
        board_width = self._outline.width
        offset_left = self._ref_point.offset_from_board(Corner.TOP_LEFT)
        offset_right = self._ref_point.offset_from_board(Corner.TOP_RIGHT)

        self._logger.info(f"Board幅: {board_width:.3f} mm")
        self._logger.info(f"左オフセット: ({offset_left.x:.3f}, {offset_left.y:.3f})")
        self._logger.info(f"右オフセット: ({offset_right.x:.3f}, {offset_right.y:.3f})")

        move_velocity = stage.max_velocity * self._move_velocity_ratio

        # top left reference pointへ移動
        top_left_ref = self._ref_point.get_reference_position(Corner.TOP_LEFT)
        self._logger.info("=== Top Left Reference Pointへ移動 ===")
        self._logger.info(f"目標位置: ({top_left_ref.x:.3f}, {top_left_ref.y:.3f})")
        self._move_to(
            klipper,
            stage.move(
                Trajectory(
                    stage.get_position(),
                    move_velocity,
                    [Move.from_point(top_left_ref)],
                )
            ),
        )

        # top leftで位置補正
        self._logger.info("=== Top Left Reference Pointの位置補正 ===")
        pos_left = adjust_reference()
        self._logger.info(f"Top Left位置: ({pos_left.x:.4f}, {pos_left.y:.4f})")

        # top rightへ移動（理論値）
        expected_move = Point2d(
            x=board_width - offset_left.x + offset_right.x,
            y=0.0,
        )
        self._logger.info("=== Top Right Reference Pointへ移動 ===")
        self._logger.info(
            f"理論移動距離: ({expected_move.x:.3f}, {expected_move.y:.3f})"
        )

        target_pos = pos_left + expected_move
        self._move_to(
            klipper,
            stage.move(
                Trajectory(
                    stage.get_position(),
                    move_velocity,
                    [Move.from_point(target_pos)],
                )
            ),
        )

        # top rightで位置補正
        self._logger.info("=== Top Right Reference Pointの位置補正 ===")
        pos_right = adjust_reference()
        self._logger.info(f"Top Right位置: ({pos_right.x:.4f}, {pos_right.y:.4f})")

        # 回転を計算
        actual_move = pos_right - pos_left
        rotation = Rotation.from_points(expected_move, actual_move)
        self._logger.info(f"計測された回転角: {rotation.degrees:.4f}°")

        # Board原点の機械座標を計算
        board_origin = pos_left - rotation.apply(offset_left)
        self._logger.info(
            f"Board左上コーナーの機械座標: ({board_origin.x:.4f}, {board_origin.y:.4f})"
        )

        # 変換を構成
        transform = Compose([rotation, Translation.from_point(board_origin)])

        # Board左上コーナーへ移動
        self._logger.info("=== Board左上コーナーへ移動 ===")
        self._move_to(
            klipper,
            stage.move(
                Trajectory(
                    stage.get_position(),
                    move_velocity,
                    [Move.from_point(board_origin)],
                )
            ),
        )

        self._logger.info("Board変換の計測完了")
        return transform

    def _move_to(
        self,
        klipper: Klipper,
        move_gcode: gcode.GCode,
    ) -> None:
        """指定座標に移動し、安定を待つ."""
        klipper.send_gcode(
            move_gcode + gcode.wait(self._settle_time) + gcode.wait_for_done()
        )
