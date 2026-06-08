"""電気接触式プローブのHAL."""

from __future__ import annotations

from pcbasm.gcode import GCode, wait

from .klipper import ReadonlyKlipper
from .servo import Servo


class ProbeSensor:
    """Klipperの[probe]セクションを利用した電気接触式プローブセンサー.

    Example:
        klipper = Klipper()
        probe = ProbeSensor(klipper.readonly)
        # PROBE gcodeを実行後
        z = probe.get_last_z_result()
    """

    def __init__(self, klipper: ReadonlyKlipper) -> None:
        """ProbeSensorを初期化する.

        Args:
            klipper: Klipperクライアント

        Raises:
            RuntimeError: printer.cfgに[probe]セクションがない場合
        """
        config = klipper.get_config()
        if "probe" not in config:
            raise RuntimeError("printer.cfgに[probe]セクションを追加してください")
        self._klipper = klipper

    def get_last_z_result(self) -> float:
        """最後のプローブ計測のZ座標を返す.

        Returns:
            プローブが接触したZ座標 (mm)
        """
        return float(self._klipper.get_status("probe", "last_z_result"))


class ProbeGround:
    """サーボでプローブ用グラウンドピンを上下させるHAL.

    「一回転あたりの移動量」と「下げる距離」からサーボ角度を算出する。
    up()は常に角度0、down(distance)は`distance / revolution_distance * 360`度。
    """

    def __init__(
        self,
        klipper: ReadonlyKlipper,
        servo_name: str,
        revolution_distance: float,
    ) -> None:
        """ProbeGroundを初期化する.

        Args:
            klipper: Klipperクライアント
            servo_name: printer.cfgの[servo <name>]のname部分
            revolution_distance: サーボ一回転あたりの移動量 [mm]
        """
        self._servo = Servo(klipper, servo_name)
        self._revolution_distance = revolution_distance

    def down(self, distance: float) -> GCode:
        """グラウンドを指定距離だけ下げるGCodeを返す.

        Args:
            distance: グラウンドを下げる距離 [mm]
        """
        return self._servo.set_angle(distance / self._revolution_distance * 360.0)

    def up(self) -> GCode:
        """グラウンドを上げるGCodeを返す."""
        return self._servo.set_angle(0.0)


class ServoGroundProbe:
    """ProbeSensorとProbeGroundを統合した公開HAL.

    Example:
        klipper = Klipper()
        probe = ServoGroundProbe(klipper.readonly, "probe_gnd", 40.0, 5.0)
        klipper.send_gcode(probe.probe())
        z = probe.get_last_z_result()
    """

    def __init__(
        self,
        klipper: ReadonlyKlipper,
        servo_name: str,
        revolution_distance: float,
        down_distance: float,
        down_settle_time: float = 0.5,
    ) -> None:
        """ServoGroundProbeを初期化する.

        Args:
            klipper: Klipperクライアント
            servo_name: グラウンド用サーボのname
            revolution_distance: サーボ一回転あたりの移動量 [mm]
            down_distance: グラウンドを下げる距離 [mm]
            down_settle_time: down後、PROBE実行までサーボ可動を待つ時間 [秒]
        """
        self._sensor = ProbeSensor(klipper)
        self._ground = ProbeGround(klipper, servo_name, revolution_distance)
        self._down_distance = down_distance
        self._down_settle_time = down_settle_time

    def probe(self) -> GCode:
        """グラウンドを下げ、可動を待ってPROBEを実行し、グラウンドを上げる一連のGCodeを返す.

        SET_SERVOは即座に完了扱いになるため、down後にサーボが物理的に下がりきる前に
        PROBEが実行されないよう、down_settle_timeだけdwell(G4)を挟む。
        """
        return (
            self._ground.down(self._down_distance)
            + wait(self._down_settle_time)
            + GCode("PROBE")
            + self._ground.up()
        )

    def get_last_z_result(self) -> float:
        """最後のプローブ計測のZ座標を返す.

        Returns:
            プローブが接触したZ座標 (mm)
        """
        return self._sensor.get_last_z_result()
