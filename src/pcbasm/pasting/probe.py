"""PROBEコマンドを実行するクラス."""

from pcbasm.gcode import GCode
from pcbasm.hal import Klipper, XYZStage


class ProbeExecutor:
    """PROBEコマンドを実行し、接触Z座標を取得するクラス.

    Klipperの[load_cell_probe]セクションを利用する。
    PROBE実行後、接触点からlift_height分だけZ軸を持ち上げる。
    """

    def __init__(
        self,
        klipper: Klipper,
        stage: XYZStage,
        *,
        lift_height: float = 1.0,
        settle_sec: float,
    ) -> None:
        """ProbeExecutorを初期化する.

        Args:
            klipper: Klipperクライアント
            stage: XYZステージ
            lift_height: PROBE実行後に接触点から持ち上げる高さ [mm]
            settle_sec: PROBE実行後の待ち [sec]（``machine.settle.probe_sec``）

        Raises:
            RuntimeError: printer.cfgに[load_cell_probe]セクションがない場合
        """
        config = klipper.readonly.get_config()
        if "load_cell_probe" not in config:
            raise RuntimeError(
                "printer.cfgに[load_cell_probe]セクションを追加してください"
            )
        self._klipper = klipper
        self._stage = stage
        self._lift_height = lift_height
        self._settle_sec = settle_sec

    def probe(self) -> float:
        """PROBEを実行し、接触したZ座標を返す.

        PROBE実行後、接触点から lift_height 分だけZ軸を持ち上げる。

        Returns:
            接触したZ座標 (mm)
        """
        self._klipper.send_gcode(
            GCode("PROBE") + GCode.wait(self._settle_sec) + GCode.wait_for_done()
        )
        z = float(self._klipper.readonly.get_status("probe", "last_z_result"))
        self._klipper.send_gcode(
            self._stage.move(z=z + self._lift_height) + GCode.wait_for_done()
        )
        return z
