from __future__ import annotations

from pcbasm.gcode import GCode

from .klipper import ReadonlyKlipper

_PIN_NAME = "air_pump"
_CONFIG_SECTION = f"output_pin {_PIN_NAME}"


class AirPump:
    """エアポンプのHAL.

    ``on`` / ``off`` は G-code を返すだけで送信しない。
    """

    def __init__(self, klipper: ReadonlyKlipper) -> None:
        """Klipper 設定に air_pump の出力ピンがあるか確かめる.

        ``[output_pin air_pump]`` が無ければ RuntimeError を送出する。
        """
        self._klipper = klipper
        self._check_klipper()

    def _check_klipper(self) -> None:
        config = self._klipper.get_config()
        if _CONFIG_SECTION not in config:
            raise RuntimeError(f"printer.cfgに[{_CONFIG_SECTION}]を追加してください")

    def on(self) -> GCode:
        return GCode(f"SET_PIN PIN={_PIN_NAME} VALUE=1")

    def off(self) -> GCode:
        return GCode(f"SET_PIN PIN={_PIN_NAME} VALUE=0")
