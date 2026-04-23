"""電気接触式プローブのHAL."""

from __future__ import annotations

from pcb_assembly.gcode import GCode

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

    「一回転あたりの移動量」と「下げる距離」からサーボ角度を算出する。 up()は常に角度0、down()は`down_distance /
    revolution_distance * 360`度。
    """

    def __init__(
        self,
        klipper: ReadonlyKlipper,
        servo_name: str,
        revolution_distance: float,
        down_distance: float,
    ) -> None:
        """ProbeGroundを初期化する.

        Args:
            klipper: Klipperクライアント
            servo_name: printer.cfgの[servo <name>]のname部分
            revolution_distance: サーボ一回転あたりの移動量 [mm]
            down_distance: グラウンドを下げる距離 [mm]
        """
        self._servo = Servo(klipper, servo_name)
        self._down_angle = down_distance / revolution_distance * 360.0

    def down(self) -> GCode:
        """グラウンドを下げるGCodeを返す."""
        return self._servo.set_angle(self._down_angle)

    def up(self) -> GCode:
        """グラウンドを上げるGCodeを返す."""
        return self._servo.set_angle(0.0)


class Probe:
    """ProbeSensorとProbeGroundを統合した公開HAL.

    Example:
        klipper = Klipper()
        probe = Probe(klipper.readonly, "probe_gnd", 40.0, 5.0)
        klipper.send_gcode(probe.probe())
        z = probe.get_last_z_result()
    """

    def __init__(
        self,
        klipper: ReadonlyKlipper,
        servo_name: str,
        revolution_distance: float,
        down_distance: float,
    ) -> None:
        """Probeを初期化する.

        Args:
            klipper: Klipperクライアント
            servo_name: グラウンド用サーボのname
            revolution_distance: サーボ一回転あたりの移動量 [mm]
            down_distance: グラウンドを下げる距離 [mm]
        """
        self._sensor = ProbeSensor(klipper)
        self._ground = ProbeGround(
            klipper, servo_name, revolution_distance, down_distance
        )

    def probe(self) -> GCode:
        """グラウンドを下げ、PROBEを実行し、グラウンドを上げる一連のGCodeを返す."""
        return self._ground.down() + GCode("PROBE") + self._ground.up()

    def get_last_z_result(self) -> float:
        """最後のプローブ計測のZ座標を返す.

        Returns:
            プローブが接触したZ座標 (mm)
        """
        return self._sensor.get_last_z_result()
