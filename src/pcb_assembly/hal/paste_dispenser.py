import math

from .klipper import GCode, ReadonlyKlipper


class PasteDispenser:
    """はんだペーストディスペンサーのHAL.

    マイクロリットル [μL] 単位のAPIを提供します。
    """

    def __init__(
        self,
        klipper: ReadonlyKlipper,
        syringe_size: float,
        stepper_name: str = "paste_dispenser",
    ) -> None:
        """PasteDispenserを初期化する.

        Args:
            klipper: Klipperクライアント
            syringe_size: シリンジの直径 [mm]
            stepper_name: manual_stepperの名前

        Raises:
            RuntimeError: printer.cfgにmanual_stepperセクションがない場合
        """
        self._klipper = klipper
        self._stepper_name = stepper_name
        self._check_klipper()
        self._syringe_area = math.pi * (syringe_size / 2) ** 2
        self._reset_pos = GCode(f"{self._cmd_prefix} SET_POSITION=0")

    @property
    def _stepper_section(self) -> str:
        return f"manual_stepper {self._stepper_name}"

    @property
    def _cmd_prefix(self) -> str:
        return f"MANUAL_STEPPER STEPPER={self._stepper_name}"

    def _check_klipper(self) -> None:
        config = self._klipper.get_config()
        if self._stepper_section not in config:
            raise RuntimeError(
                f"printer.cfgに[{self._stepper_section}]を追加してください"
            )

    def _microl_to_mm(self, microl: float) -> float:
        """マイクロリットル単位をミリメートル距離に変換."""
        return microl / self._syringe_area

    def dispense(self, rate: float, accel: float) -> GCode:
        """連続ディスペンスを開始するGCodeを生成.

        Args:
            rate: 吐出速度 [μL/sec]
            accel: 加速度 [μL/sec²]

        Returns:
            ディスペンス開始用のGCode
        """
        speed_mm = self._microl_to_mm(rate)
        accel_mm = self._microl_to_mm(accel)
        # MOVE=1000は十分大きな値（stopで停止するまで継続）
        gcode = self._reset_pos.copy()
        gcode.append(f"{self._cmd_prefix} MOVE=1000 SPEED={speed_mm} ACCEL={accel_mm}")
        return gcode

    def retract(self, amount: float, rate: float, accel: float) -> GCode:
        """リトラクション（吸い戻し）のGCodeを生成.

        Args:
            amount: リトラクション量 [μL]
            rate: リトラクション速度 [μL/sec]
            accel: 加速度 [μL/sec²]

        Returns:
            リトラクション用のGCode
        """
        distance_mm = self._microl_to_mm(amount)
        speed_mm = self._microl_to_mm(rate)
        accel_mm = self._microl_to_mm(accel)
        gcode = self._reset_pos.copy()
        gcode.append(
            f"{self._cmd_prefix} MOVE=-{distance_mm} SPEED={speed_mm} ACCEL={accel_mm}"
        )
        return gcode

    def stop(self) -> GCode:
        """ディスペンスを停止するGCodeを生成."""
        return GCode(
            [
                f"{self._cmd_prefix} ENABLE=0",
                f"{self._cmd_prefix} ENABLE=1",
            ]
        )
