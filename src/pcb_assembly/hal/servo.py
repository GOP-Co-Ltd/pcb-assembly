from __future__ import annotations

from pcb_assembly.gcode import GCode

from .klipper import ReadonlyKlipper


class Servo:
    """サーボモーターのHAL."""

    def __init__(self, klipper: ReadonlyKlipper, name: str = "probe_gnd") -> None:
        self._klipper = klipper
        self._name = name
        self._section = f"servo {name}"
        self._check_klipper()

    def _check_klipper(self) -> None:
        config = self._klipper.get_config()
        if self._section not in config:
            raise RuntimeError(f"printer.cfgに[{self._section}]を追加してください")

    def set_angle(self, angle: float) -> GCode:
        return GCode(f"SET_SERVO SERVO={self._name} ANGLE={angle}")
