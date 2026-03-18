"""PROBEコマンドを実行するクラス."""

from pcb_assembly import gcode
from pcb_assembly.hal import Klipper, ProbeSensor


class ProbeExecutor:
    """PROBEコマンドを実行し、接触Z座標を取得するクラス.

    PROBE実行後、接触点からlift_height分だけZ軸を持ち上げる。
    """

    def __init__(
        self,
        klipper: Klipper,
        probe: ProbeSensor,
        *,
        lift_height: float = 5.0,
        settle_time: float = 1.0,
        velocity: float = 20.0,
    ) -> None:
        self._klipper = klipper
        self._probe = probe
        self._lift_height = lift_height
        self._settle_time = settle_time
        self._velocity = velocity

    def probe(self) -> float:
        """PROBEを実行し、接触したZ座標を返す.

        PROBE実行後、接触点から lift_height 分だけZ軸を持ち上げる。

        Returns:
            接触したZ座標 (mm)
        """
        self._klipper.send_gcode(
            gcode.GCode("PROBE") + gcode.wait(self._settle_time) + gcode.wait_for_done()
        )
        z = self._probe.get_last_z_result()
        self._klipper.send_gcode(
            gcode.move(z=z + self._lift_height, velocity=self._velocity)
            + gcode.wait_for_done()
        )
        return z
