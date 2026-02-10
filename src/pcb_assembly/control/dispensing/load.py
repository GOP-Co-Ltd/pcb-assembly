"""ペーストのローディング処理."""

import logging

from pcb_assembly import gcode
from pcb_assembly.hal import Klipper, PasteDispenser
from pcb_assembly.utils import get_class_module_path


class PasteLoader:
    """はんだペーストのローディングを制御するクラス.

    PasteDispenserが生成するGCodeを送信し、完了まで待機する。

    Example:
        loader = PasteLoader(klipper, paste_dispenser, rate=1.0, accel=1.0)
        loader.load(amount=2.0)  # 2μL押し出し（ブロッキング）
    """

    def __init__(
        self,
        klipper: Klipper,
        paste_dispenser: PasteDispenser,
        rate: float,
        accel: float,
    ) -> None:
        """PasteLoaderを初期化する.

        Args:
            klipper: Klipperクライアント
            paste_dispenser: ペーストディスペンサーHAL
            rate: ローディング速度 [μL/sec]
            accel: ローディング加速度 [μL/sec²]
        """
        self._klipper = klipper
        self._paste_dispenser = paste_dispenser
        self._rate = rate
        self._accel = accel
        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def load(self, amount: float) -> None:
        """指定量のペーストを押し出す.

        GCodeを生成・送信し、動作完了まで待機（ブロッキング）する。

        Args:
            amount: 押し出し量 [μL]（正: 吐出、負: リトラクション）
        """
        self._logger.info(f"ペーストローディング: {amount} μL")
        push_gcode = self._paste_dispenser.pushpull(amount, self._rate, self._accel)
        self._klipper.send_gcode(push_gcode + gcode.wait_for_done())
        self._logger.info("ローディング完了")
