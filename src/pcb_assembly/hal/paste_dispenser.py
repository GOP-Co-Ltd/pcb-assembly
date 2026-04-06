# TODO: オーガースクリュー＋エアアシスト方式に合わせて再実装する

# import math
#
# import attrs
#
# from .klipper import GCode, ReadonlyKlipper
#
#
# @attrs.frozen
# class NozzleSpec:
#     """ディスペンサーノズルの仕様.
#
#     Attributes:
#         inner_diameter: 内径 [mm]
#     """
#
#     inner_diameter: float
#
#
# NOZZLE_SPECS: dict[str, NozzleSpec] = {
#     "27G": NozzleSpec(inner_diameter=0.19),
# }
#
#
# class PasteDispenser:
#     """はんだペーストディスペンサーのHAL.
#
#     マイクロリットル [μL] 単位のAPIを提供します。
#     """
#
#     def __init__(
#         self,
#         klipper: ReadonlyKlipper,
#         syringe_size: float,
#         stepper_name: str = "paste_dispenser",
#     ) -> None:
#         """PasteDispenserを初期化する.
#
#         Args:
#             klipper: Klipperクライアント
#             syringe_size: シリンジの直径 [mm]
#             stepper_name: manual_stepperの名前
#
#         Raises:
#             RuntimeError: printer.cfgにmanual_stepperセクションがない場合
#         """
#         self._klipper = klipper
#         self._stepper_name = stepper_name
#         self._check_klipper()
#         self._syringe_area = math.pi * (syringe_size / 2) ** 2
#         self._reset_pos = GCode(f"{self._cmd_prefix} SET_POSITION=0")
#
#     @property
#     def _stepper_section(self) -> str:
#         return f"manual_stepper {self._stepper_name}"
#
#     @property
#     def _cmd_prefix(self) -> str:
#         return f"MANUAL_STEPPER STEPPER={self._stepper_name}"
#
#     def _check_klipper(self) -> None:
#         config = self._klipper.get_config()
#         if self._stepper_section not in config:
#             raise RuntimeError(
#                 f"printer.cfgに[{self._stepper_section}]を追加してください"
#             )
#
#     def _microl_to_mm(self, microl: float) -> float:
#         """マイクロリットル単位をミリメートル距離に変換."""
#         return microl / self._syringe_area
#
#     def pushpull(
#         self, amount: float, rate: float, accel: float, *, sync: bool = True
#     ) -> GCode:
#         """シリンジを押し出すGCodeを生成.
#
#         Args:
#             amount: 押し出し量 [μL]（正: 吐出、負: リトラクション）
#             rate: 速度 [μL/sec]
#             accel: 加速度 [μL/sec²]
#             sync: Trueの場合、動作完了まで待機する（デフォルト: True）
#
#         Returns:
#             押し出し用のGCode
#         """
#         distance_mm = self._microl_to_mm(amount)
#         speed_mm = self._microl_to_mm(rate)
#         accel_mm = self._microl_to_mm(accel)
#         cmd = f"{self._cmd_prefix} MOVE={distance_mm} SPEED={speed_mm} ACCEL={accel_mm}"
#         if not sync:
#             cmd += " SYNC=0"
#         gcode = self._reset_pos.copy()
#         gcode.append(cmd)
#         return gcode
