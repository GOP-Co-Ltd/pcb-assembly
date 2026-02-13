"""ペースト塗布の制御."""

import logging
from collections.abc import Iterable

from shapely import Polygon

from pcb_assembly import gcode
from pcb_assembly.geometry import (
    Identity,
    Move,
    Point2d,
    Trajectory,
    Transform,
    generate_fill_path,
)
from pcb_assembly.hal import Klipper, NozzleSpec, PasteDispenser, XYZStage
from pcb_assembly.utils import get_class_module_path


class PasteApplicator:
    """ポリゴンへのペースト塗布を制御するクラス.

    ペーストのローディング・リトラクション・塗布を一貫して提供する。
    fill pathを生成し、ディスペンサー押出しとXYZステージ移動を
    同期させたGCodeシーケンスを生成・送信する。

    Example:
        applicator = PasteApplicator(
            klipper=klipper,
            paste_dispenser=dispenser,
            stage=stage,
            nozzle_spec=NozzleSpec(inner_diameter=0.19),
            paste_velocity=10.0,
            paste_thickness=0.1,
            retraction=10.0,
            retraction_rate=10.0,
            retraction_accel_factor=2.0,
            paste_accel=1.0,
            paste_height=0.5,
        )
        applicator.load(2.0)
        applicator.retract()
        applicator.apply([polygon])
    """

    def __init__(
        self,
        klipper: Klipper,
        paste_dispenser: PasteDispenser,
        stage: XYZStage,
        nozzle_spec: NozzleSpec,
        paste_velocity: float,
        paste_thickness: float,
        retraction: float,
        retraction_rate: float,
        retraction_accel_factor: float,
        paste_accel: float,
        transform: Transform = Identity(),
        paste_height: float = 0.1,
        lift_height: float = 5.0,
        overlap_ratio: float = 0.0,
    ) -> None:
        """PasteApplicatorを初期化する.

        Args:
            klipper: Klipperクライアント
            paste_dispenser: ペーストディスペンサーHAL
            stage: XYZステージ
            nozzle_spec: ノズル仕様
            paste_velocity: ペースト時のXYステージ移動速度 [mm/s]
            paste_thickness: ペースト膜厚 [mm]
            retraction: リトラクション量 [μL]
            retraction_rate: リトラクション速度 [μL/sec]
            retraction_accel_factor: リトラクション加速度係数（>1.0）
            paste_accel: ペースト吐出加速度 [μL/sec²]
            transform: 座標変換
            paste_height: 塗布面のZ高さ [mm]
            lift_height: 塗布後の上昇高さ [mm]
            overlap_ratio: 走査線のオーバーラップ率（0.0〜1.0未満）

        Raises:
            ValueError: overlap_ratioが範囲外の場合
            ValueError: retraction_accel_factorが1.0以下の場合
        """
        if not (0.0 <= overlap_ratio < 1.0):
            raise ValueError(
                f"overlap_ratioは0.0以上1.0未満である必要があります: {overlap_ratio}"
            )
        if retraction_accel_factor <= 1.0:
            raise ValueError(
                f"retraction_accel_factorは1.0より大きい必要があります: "
                f"{retraction_accel_factor}"
            )

        self._klipper = klipper
        self._paste_dispenser = paste_dispenser
        self._stage = stage
        self._nozzle_spec = nozzle_spec
        self._paste_velocity = paste_velocity
        self._paste_thickness = paste_thickness
        self._transform = transform
        self._paste_height = paste_height
        self._retraction = retraction
        self._retraction_rate = retraction_rate
        self._retraction_accel_factor = retraction_accel_factor
        self._paste_accel = paste_accel
        self._lift_height = lift_height
        self._overlap_ratio = overlap_ratio
        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    @property
    def _paste_rate(self) -> float:
        """ペースト吐出速度 r_p [μL/sec]."""
        return (
            self._paste_velocity
            * self._nozzle_spec.inner_diameter
            * self._paste_thickness
        )

    @property
    def _retraction_accel(self) -> float:
        """リトラクション加速度 a_R [μL/sec²]."""
        return (
            self._retraction_accel_factor * self._retraction_rate**2 / self._retraction
        )

    @property
    def _wait_time(self) -> float:
        """吐出開始後の待機時間 T_w [sec]."""
        return self._paste_rate / self._paste_accel

    def load(self, amount: float) -> None:
        """指定量のペーストを押し出す.

        GCodeを生成・送信し、動作完了まで待機（ブロッキング）する。

        Args:
            amount: 押し出し量 [μL]（正: 吐出、負: リトラクション）
        """
        self._logger.info(f"ペーストローディング: {amount} μL")
        push_gcode = self._paste_dispenser.pushpull(
            amount, self._retraction_rate, self._retraction_accel
        )
        self._klipper.send_gcode(push_gcode + gcode.wait_for_done())
        self._logger.info("ローディング完了")

    def retract(self) -> None:
        """リトラクションを実行する.

        retraction量分だけペーストを引き戻す（ブロッキング）。
        """
        self.load(-self._retraction)

    def apply(
        self,
        polygons: Iterable[Polygon],
        perimeters: int = 1,
        angle: float = 0.0,
    ) -> None:
        """複数ポリゴンへペースト塗布を実行する.

        各ポリゴンに対してGCodeを生成し、Klipperに送信する（ブロッキング）。

        Args:
            polygons: 塗りつぶし対象のポリゴン群
            perimeters: 外周の周回数
            angle: ジグザグ走査線の角度（度）

        Raises:
            ValueError: fill pathが空の場合、または経路が制限外の場合
        """
        for polygon in polygons:
            gc = self._generate(polygon, perimeters, angle)
            self._klipper.send_gcode(gc)

    def _generate(
        self,
        polygon: Polygon,
        perimeters: int = 1,
        angle: float = 0.0,
    ) -> gcode.GCode:
        """ポリゴンから塗布GCodeを生成する.

        ノズル仕様に基づいてfill pathを生成し、ディスペンサー押出しと
        XYZステージ移動を同期させた完全なGCodeシーケンスを返す。

        Args:
            polygon: 塗りつぶし対象のポリゴン
            perimeters: 外周の周回数
            angle: ジグザグ走査線の角度（度）

        Returns:
            塗布用のGCodeシーケンス

        Raises:
            ValueError: fill pathが空の場合、または経路が制限外の場合
        """
        path = self._generate_fill_path(polygon, perimeters, angle)
        if not path:
            raise ValueError("ポリゴンから有効な塗布経路を生成できませんでした")
        return self._generate_from_path(path)

    def _generate_fill_path(
        self,
        polygon: Polygon,
        perimeters: int,
        angle: float,
    ) -> list[Point2d]:
        """ノズル仕様からfill pathを生成する."""
        d = self._nozzle_spec.inner_diameter
        line_spacing = d * (1.0 - self._overlap_ratio)
        inset = d / 2.0
        return generate_fill_path(
            polygon, line_spacing, perimeters=perimeters, inset=inset, angle=angle
        )

    def _generate_from_path(self, path: list[Point2d]) -> gcode.GCode:
        """Fill pathから塗布GCodeを生成する."""

        # 経路からTrajectoryを生成し所要時間を計算
        points = [self._transform.apply(p.to3d(self._paste_height)) for p in path]
        origin = points[0]
        trajectory = Trajectory(
            map(Move.from_point, points),
            origin=origin,
            initial_velocity=self._paste_velocity,
        )
        move_time = trajectory.time()

        # 吐出量の計算: V_d = t_p * r_p + r_p² / a_p
        r_p = self._paste_rate
        dispensing_amount = move_time * r_p + r_p**2 / self._paste_accel

        # GCodeシーケンス生成
        gc = gcode.GCode()
        lifted_z = origin.z + self._lift_height

        # 1. 始点XYへ移動（Z は paste_height + lift_height）
        gc.append(
            self._stage.to_gcode(
                Move(x=origin.x, y=origin.y, z=lifted_z, v=self._stage.max_velocity)
            )
        )

        # 2. paste_height まで下降
        gc.append(self._stage.to_gcode(Move(z=origin.z, v=self._paste_velocity)))
        gc.append(gcode.wait_for_done())

        # 3. プライム（ブロッキング）: pushpull(V_R, r_R, a_R)
        gc.append(
            self._paste_dispenser.pushpull(
                self._retraction, self._retraction_rate, self._retraction_accel
            )
        )

        # 4. 吐出開始（ノンブロッキング）: pushpull(V_d, r_p, a_p, sync=False)
        gc.append(
            self._paste_dispenser.pushpull(
                dispensing_amount, r_p, self._paste_accel, sync=False
            )
        )

        # 5. 待機 T_w 秒
        gc.append(gcode.wait(self._wait_time))

        # 6. XYZ経路移動（吐出と同時進行）
        gc.append(self._stage.to_gcode(trajectory))

        # 7. 移動完了待ち
        gc.append(gcode.wait_for_done())

        # 8. リトラクション（ブロッキング）: pushpull(-V_R, r_R, a_R)
        gc.append(
            self._paste_dispenser.pushpull(
                -self._retraction, self._retraction_rate, self._retraction_accel
            )
        )

        # 9. 上昇してペースト切り
        gc.append(self._stage.to_gcode(Move(z=lifted_z, v=self._stage.max_velocity)))

        return gc
