"""オフセット値を補正する."""

import logging
from collections.abc import Callable

from pcbasm import gcode
from pcbasm.geometry import Point2d, Rotation, Transform
from pcbasm.hal import Klipper, Speed, XYZStage
from pcbasm.utils import get_class_module_path


class OffsetTransformMeasurer:
    """観測座標系から機械座標系への変換を計測するクラス.

    2点法を用いて、機械座標系での移動ベクトルと観測座標系での
    オフセットの差分から回転角を計算する。

    Example:
        from pcbasm.vision import safe_move_distance

        move_distance = safe_move_distance(roi_size_mm)
        measurer = OffsetTransformMeasurer(
            observe=observer.observe,
            klipper=klipper,
            stage=stage,
            move_distance=move_distance,
        )
        transform = measurer.measure()
        corrected_offset = transform.apply(offset)
    """

    def __init__(
        self,
        observe: Callable[[], Transform],
        klipper: Klipper,
        stage: XYZStage,
        move_distance: float,
        move_velocity_ratio: float = 0.5,
        settle_time: float = 0.5,
    ) -> None:
        """OffsetTransformMeasurerを初期化する.

        Args:
            observe: 想定→観測のTransform（カメラmm空間）を返す関数
            klipper: Klipperクライアント
            stage: XYZステージ
            move_distance: X方向への移動距離（mm）
            move_velocity_ratio: 最大速度に対する移動速度の割合 (0.0-1.0)
            settle_time: 移動後の安定待機時間（秒）
        """
        self._observe = observe
        self._klipper = klipper
        self._stage = stage
        self._move_distance = move_distance
        self._move_velocity_ratio = move_velocity_ratio
        self._settle_time = settle_time

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def measure(self) -> Transform:
        """2点法で回転変換を計測する.

        処理手順:
        1. 現在位置で対象を検出しオフセット o1 を取得
        2. X方向に move_distance だけ移動
        3. 移動後に検出しオフセット o2 を取得
        4. 元の位置に戻る
        5. 移動ベクトルと (o1 - o2) の角度差から回転を計算

        Returns:
            観測座標系から機械座標系への回転変換
        """
        self._logger.info("オフセット補正の計測を開始")

        # 1. 現在位置で検出
        o1 = self._observe().apply(Point2d(0.0, 0.0))
        start_pos = self._stage.get_position()
        self._logger.info(
            f"初期位置: {start_pos}, オフセット o1: ({o1.x:.4f}, {o1.y:.4f}) mm"
        )

        # 2. X方向に移動
        move_vector = Point2d(x=self._move_distance, y=0.0)
        move_velocity = self._stage.max_velocity * self._move_velocity_ratio
        self._move_to(
            self._stage.move(
                x=move_vector.x,
                y=move_vector.y,
                speed=Speed.absolute(move_velocity),
                relative=True,
            ),
        )

        # 3. 移動後に検出
        o2 = self._observe().apply(Point2d(0.0, 0.0))
        self._logger.info(f"オフセット o2: ({o2.x:.4f}, {o2.y:.4f}) mm")

        # 4. 元の位置に戻る
        self._logger.info("元の位置に戻る")
        self._move_to(
            self._stage.move(
                x=start_pos.x,
                y=start_pos.y,
                z=start_pos.z,
                speed=Speed.absolute(move_velocity),
            ),
        )

        # 5. 回転角を計算
        # 観測されるオフセット変化 (o2 - o1) は、機械の移動方向 move_vector に対応
        rotation = Rotation.from_points(move_vector, o2 - o1)
        self._logger.info(f"計測完了: 回転角 {rotation.degrees:.4f}°")

        return rotation

    def _move_to(self, move_gcode: gcode.GCode) -> None:
        """指定座標に移動し、安定を待つ."""
        self._klipper.send_gcode(
            move_gcode + gcode.wait(self._settle_time) + gcode.wait_for_done()
        )
