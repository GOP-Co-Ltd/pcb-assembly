"""計測処理を記述するモジュール."""

import logging

from pcb_assembly import gcode
from pcb_assembly.geometry import Move, Point2d, Rotation, Trajectory
from pcb_assembly.hal import Camera, Klipper, XYZStage
from pcb_assembly.utils import get_class_module_path
from pcb_assembly.vision import CircleDetector


class CameraRotationMeasurer:
    """カメラ座標系と機械座標系の回転ずれを計測するクラス.

    2点法を用いて、機械座標系での移動ベクトルとカメラ座標系での
    観測オフセットの差分から回転角を計算する。

    Example:
        move_distance = CameraRotationMeasurer.compute_move_distance(crop_size)
        measurer = CameraRotationMeasurer(camera, detector, klipper, stage, move_distance)
        rotation = measurer.measure()
    """

    @staticmethod
    def compute_move_distance(crop_size: Point2d, ratio: float = 0.8) -> float:
        """crop_sizeから適切な移動距離を計算する.

        検出領域内で計測できるよう、crop_sizeの短辺に対する割合から
        片方向の移動距離を算出する。

        Args:
            crop_size: 検出領域のサイズ (mm)
            ratio: crop_sizeの短辺に対する移動範囲の割合（デフォルト: 0.8）

        Returns:
            片方向の移動距離 (mm)
        """
        return min(crop_size.x, crop_size.y) * ratio / 2

    def __init__(
        self,
        camera: Camera,
        detector: CircleDetector,
        klipper: Klipper,
        stage: XYZStage,
        move_distance: float,
        move_velocity: float = 10.0,
        sample_count: int = 30,
        settle_time: float = 0.5,
    ) -> None:
        """CameraRotationMeasurerを初期化する.

        Args:
            camera: カメラオブジェクト
            detector: 円検出器
            klipper: Klipperクライアント
            stage: XYZステージ
            move_velocity: 移動速度 (mm/s)
            move_distance: X方向への移動距離（mm）
            sample_count: 1回の検出に使用する画像枚数
            settle_time: 移動後の安定待機時間（秒）
        """
        self._camera = camera
        self._detector = detector
        self._klipper = klipper
        self._stage = stage
        self._move_velocity = move_velocity
        self._move_distance = move_distance
        self._sample_count = sample_count
        self._settle_time = settle_time

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def _create_trajectory(self) -> Trajectory:
        return Trajectory(self._stage.get_position(), self._move_velocity)

    def measure(self) -> Rotation:
        """2点法で回転角を計測する.

        処理手順:
        1. 現在位置で対象を検出しオフセット o1 を取得
        2. X方向に move_distance だけ移動
        3. 移動後に検出しオフセット o2 を取得
        4. 元の位置に戻る
        5. 移動ベクトルと (o1 - o2) の角度差から回転を計算

        Returns:
            カメラ座標系から機械座標系への回転変換

        Raises:
            RuntimeError: 検出に失敗した場合
        """
        self._logger.info("カメラ回転計測を開始")

        # 1. 現在位置で検出
        o1 = self._detect()
        start_pos = self._stage.get_position()
        self._logger.info(
            f"初期位置: {start_pos}, オフセット o1: ({o1.x:.4f}, {o1.y:.4f}) mm"
        )

        # 2. X方向に移動
        move_vector = Point2d(x=self._move_distance, y=0.0)
        trajectory = self._create_trajectory()
        trajectory.add(Move.from_point(move_vector, relative=True))
        self._logger.info(f"X方向に {self._move_distance} mm 移動")
        self._move_to(trajectory)

        # 3. 移動後に検出
        o2 = self._detect()
        self._logger.info(f"オフセット o2: ({o2.x:.4f}, {o2.y:.4f}) mm")

        # 4. 元の位置に戻る
        trajectory = self._create_trajectory()
        trajectory.add(Move.from_point(start_pos))
        self._logger.info("元の位置に戻る")
        self._move_to(trajectory)

        # 5. 回転角を計算
        # カメラで観測されるオフセット変化 (o2 - o1) は、機械の移動方向 move_vector に対応
        rotation = Rotation.from_points(move_vector, o2 - o1)
        self._logger.info(f"計測完了: 回転角 {rotation.degrees:.4f}°")

        return rotation

    def _detect(self) -> Point2d:
        """画像を取得し、オフセットを検出する."""
        self._logger.info(f"{self._sample_count}枚の画像から検出中...")
        result = self._detector.detect_with_statistics(
            self._camera.capture() for _ in range(self._sample_count)
        )
        if result is None:
            raise RuntimeError("検出に失敗しました")
        self._logger.info(
            f"検出成功: 標準偏差 ({result.std_mm.x:.4f}, {result.std_mm.y:.4f}) mm"
        )
        return result.mean_mm

    def _move_to(self, trajectory: Trajectory) -> None:
        """指定座標に移動し、安定を待つ."""
        self._klipper.send_gcode(
            self._stage.move(trajectory)
            + gcode.wait(self._settle_time)
            + gcode.wait_for_done()
        )
