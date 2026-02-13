"""ペースト塗布のGCode生成."""

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
from pcb_assembly.hal import NozzleSpec, PasteDispenser, XYZStage


class PasteApplicator:
    """ポリゴンへのペースト塗布GCodeを生成するクラス.

    fill pathを生成し、ディスペンサー押出しとXYZステージ移動を
    同期させたGCodeシーケンスを生成する。

    Example:
        applicator = PasteApplicator(
            paste_dispenser=dispenser,
            stage=stage,
            nozzle_spec=NozzleSpec(inner_diameter=0.19),
            paste_height=0.5,
            paste_velocity=10.0,
            rate=1.0,
            accel=1.0,
            retraction=10.0,
        )
        gc = applicator.generate(polygon)
        klipper.send_gcode(gc)
    """

    def __init__(
        self,
        paste_dispenser: PasteDispenser,
        stage: XYZStage,
        nozzle_spec: NozzleSpec,
        paste_velocity: float,
        rate: float,
        accel: float,
        retraction: float,
        transform: Transform = Identity(),
        paste_height: float = 0.1,
        lift_height: float = 5.0,
        overlap_ratio: float = 0.0,
    ) -> None:
        """PasteApplicatorを初期化する.

        Args:
            paste_dispenser: ペーストディスペンサーHAL
            stage: XYZステージ
            nozzle_spec: ノズル仕様
            paste_velocity: ペースト時のXYステージ移動速度 [mm/s]
            rate: ペースト吐出速度 [μL/sec]
            accel: ペースト吐出加速度 [μL/sec²]
            retraction: リトラクション量 [μL]
            paste_height: 塗布面のZ高さ [mm]
            lift_height: 塗布後の上昇高さ [mm]
            overlap_ratio: 走査線のオーバーラップ率（0.0〜1.0未満）

        Raises:
            ValueError: retractionが加速中の押出量未満の場合
            ValueError: overlap_ratioが範囲外の場合
        """
        if not (0.0 <= overlap_ratio < 1.0):
            raise ValueError(
                f"overlap_ratioは0.0以上1.0未満である必要があります: {overlap_ratio}"
            )

        self._paste_dispenser = paste_dispenser
        self._stage = stage
        self._nozzle_spec = nozzle_spec
        self._paste_velocity = paste_velocity
        self._rate = rate
        self._accel = accel
        self._transform = transform
        self._paste_height = paste_height
        self._retraction = retraction
        self._lift_height = lift_height
        self._overlap_ratio = overlap_ratio

        if retraction < self._amount_accel:
            raise ValueError(
                f"retraction ({retraction} μL) は加速中の押出量"
                f" ({self._amount_accel:.2f} μL) 以上に設定してください。"
                "定常速度に達してからXYZ移動を開始する必要があります。"
            )

    @property
    def _amount_accel(self) -> float:
        """加速中の押出量 [μL]."""
        return self._rate**2 / (2 * self._accel)

    def generate(
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

        # 押出し量の計算（台形速度プロファイル）
        total_amount = self._retraction + self._rate * move_time + self._amount_accel

        # プライム待機時間の計算
        t_accel = self._rate / self._accel
        prime_wait = t_accel + (self._retraction - self._amount_accel) / self._rate

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

        # 3. 非ブロッキングで押出し開始
        gc.append(
            self._paste_dispenser.pushpull(
                total_amount, self._rate, self._accel, sync=False
            )
        )

        # 4. リトラクション分が出るまで待機
        gc.append(gcode.wait(prime_wait))

        # 5. XYZ経路移動（押出しと同時進行）
        gc.append(self._stage.to_gcode(trajectory))

        # 6. 移動完了待ち
        gc.append(gcode.wait_for_done())

        # 7. リトラクション
        gc.append(
            self._paste_dispenser.pushpull(-self._retraction, self._rate, self._accel)
        )

        # 8. リトラクション完了待ち
        gc.append(gcode.wait_for_done())

        # 9. 上昇してペースト切り
        gc.append(self._stage.to_gcode(Move(z=lifted_z, v=self._stage.max_velocity)))

        return gc
