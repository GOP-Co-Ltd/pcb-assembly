"""PROBEコマンドを実行するクラス."""

from pcb_assembly import gcode
from pcb_assembly.geometry import Move
from pcb_assembly.hal import Klipper, Probe, XYZStage


class ProbeExecutor:
    """PROBEコマンドを実行し、接触Z座標を取得するクラス.

    PROBE実行後、接触点からlift_height分だけZ軸を持ち上げる。
    """

    def __init__(
        self,
        klipper: Klipper,
        probe: Probe,
        stage: XYZStage,
        *,
        lift_height: float = 5.0,
        settle_time: float = 1.0,
    ) -> None:
        self._klipper = klipper
        self._probe = probe
        self._stage = stage
        self._lift_height = lift_height
        self._settle_time = settle_time

    def probe(self) -> float:
        """PROBEを実行し、接触したZ座標を返す.

        PROBE実行後、接触点から lift_height 分だけZ軸を持ち上げる。

        Returns:
            接触したZ座標 (mm)
        """
        self._klipper.send_gcode(
            self._probe.probe() + gcode.wait(self._settle_time) + gcode.wait_for_done()
        )
        z = self._probe.get_last_z_result()
        self._klipper.send_gcode(
            self._stage.to_gcode(Move(z=z + self._lift_height)) + gcode.wait_for_done()
        )
        return z
