"""PROBE コマンドを実行するクラス."""

from pcbasm.gcode import GCode
from pcbasm.hal import Klipper, XYZStage


class ProbeExecutor:
    """PROBE コマンドを実行し、接触 Z 座標を取得するクラス.

    Klipper の [load_cell_probe] セクションを利用する。 PROBE 実行後、接触点から lift_height
    分だけ Z 軸を持ち上げる。
    """

    def __init__(
        self,
        klipper: Klipper,
        stage: XYZStage,
        *,
        lift_height: float = 1.0,
        settle_sec: float,
    ) -> None:
        """ProbeExecutor を初期化する.

        Args:
            klipper: Klipper クライアント
            stage: XYZ ステージ
            lift_height: PROBE 実行後に接触点から持ち上げる高さ [mm]
            settle_sec: PROBE 実行後の待ち [sec]（``machine.settle.probe_sec``）

        Raises:
            RuntimeError: printer.cfg に [load_cell_probe] セクションがない場合
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
        """PROBE を実行し、接触した Z 座標を返す.

        PROBE 実行後、接触点から lift_height 分だけ Z 軸を持ち上げる。

        Returns:
            接触した Z 座標 (mm)
        """
        self._klipper.send_gcode(
            GCode("PROBE") + GCode.wait(self._settle_sec) + GCode.wait_for_done()
        )
        z = float(self._klipper.readonly.get_status("probe", "last_z_result"))
        self._klipper.send_gcode(
            self._stage.move(z=z + self._lift_height) + GCode.wait_for_done()
        )
        return z
