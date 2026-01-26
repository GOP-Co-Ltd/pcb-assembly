"""オフセット値を補正する."""

import logging
from collections.abc import Callable

from pcb_assembly import gcode
from pcb_assembly.geometry import Move, Point2d, Rotation, Trajectory, Transform
from pcb_assembly.hal import Klipper, XYZStage
from pcb_assembly.utils import get_class_module_path


class OffsetAdjustor:
    """オフセット位置を回転などで補正するクラス.

    観測座標系と機械座標系の回転ずれを計測し、オフセット値を補正する。
    2点法を用いて、機械座標系での移動ベクトルと観測座標系での
    オフセットの差分から回転角を計算する。

    Example:
        move_distance = OffsetAdjustor.move_distance_from_crop(crop_size)
        adjustor = OffsetAdjustor(move_distance)
        transform = adjustor.measure(observe_offset, klipper, stage)
        corrected_offset = adjustor.adjust(offset)
    """

    @staticmethod
    def move_distance_from_crop(
        crop_size: tuple[float, float],
        ratio: float = 0.8,
    ) -> float:
        """crop_sizeから適切な移動距離を計算する.

        検出領域内で計測できるよう、crop_sizeの短辺に対する割合から
        片方向の移動距離を算出する。

        Args:
            crop_size: 検出領域のサイズ (width, height)
            ratio: crop_sizeの短辺に対する移動範囲の割合（デフォルト: 0.8）

        Returns:
            片方向の移動距離
        """
        return min(crop_size) * ratio / 2

    def __init__(
        self,
        move_distance: float,
        move_velocity: float = 10.0,
        settle_time: float = 0.5,
    ) -> None:
        """OffsetAdjustorを初期化する.

        Args:
            move_distance: X方向への移動距離（mm）
            move_velocity: 移動速度 (mm/s)
            settle_time: 移動後の安定待機時間（秒）
        """
        self._move_distance = move_distance
        self._move_velocity = move_velocity
        self._settle_time = settle_time

        self._transform: Transform | None = None
        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def measure(
        self,
        observe_offset: Callable[[], Point2d],
        klipper: Klipper,
        stage: XYZStage,
    ) -> Transform:
        """2点法で回転変換を計測する.

        処理手順:
        1. 現在位置で対象を検出しオフセット o1 を取得
        2. X方向に move_distance だけ移動
        3. 移動後に検出しオフセット o2 を取得
        4. 元の位置に戻る
        5. 移動ベクトルと (o1 - o2) の角度差から回転を計算

        Args:
            observe_offset: オフセットを検出して返す関数
            klipper: Klipperクライアント
            stage: XYZステージ

        Returns:
            観測座標系から機械座標系への回転変換
        """
        self._logger.info("オフセット補正の計測を開始")

        # 1. 現在位置で検出
        o1 = observe_offset()
        start_pos = stage.get_position()
        self._logger.info(
            f"初期位置: {start_pos}, オフセット o1: ({o1.x:.4f}, {o1.y:.4f}) mm"
        )

        # 2. X方向に移動
        move_vector = Point2d(x=self._move_distance, y=0.0)
        trajectory = Trajectory(stage.get_position(), self._move_velocity)
        trajectory.add(Move.from_point(move_vector, relative=True))
        self._logger.info(f"X方向に {self._move_distance} mm 移動")
        self._move_to(klipper, stage.move(trajectory))

        # 3. 移動後に検出
        o2 = observe_offset()
        self._logger.info(f"オフセット o2: ({o2.x:.4f}, {o2.y:.4f}) mm")

        # 4. 元の位置に戻る
        trajectory = Trajectory(stage.get_position(), self._move_velocity)
        trajectory.add(Move.from_point(start_pos))
        self._logger.info("元の位置に戻る")
        self._move_to(klipper, stage.move(trajectory))

        # 5. 回転角を計算
        # 観測されるオフセット変化 (o2 - o1) は、機械の移動方向 move_vector に対応
        rotation = Rotation.from_points(move_vector, o2 - o1)
        self._logger.info(f"計測完了: 回転角 {rotation.degrees:.4f}°")

        self._transform = rotation
        return rotation

    def adjust(self, offset: Point2d) -> Point2d:
        """オフセット値を補正する.

        Args:
            offset: 観測座標系でのオフセット値

        Returns:
            機械座標系に変換されたオフセット値

        Raises:
            RuntimeError: measureが実行されていない場合
        """
        if self._transform is None:
            raise RuntimeError("measureが実行されていません")
        return self._transform.apply(offset)

    def _move_to(
        self,
        klipper: Klipper,
        move_gcode: gcode.GCode,
    ) -> None:
        """指定座標に移動し、安定を待つ."""
        klipper.send_gcode(
            move_gcode + gcode.wait(self._settle_time) + gcode.wait_for_done()
        )
