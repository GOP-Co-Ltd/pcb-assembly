"""基板からの高さを計測する."""

import logging

from pcb_assembly import gcode
from pcb_assembly.geometry import Shift, Transform
from pcb_assembly.hal import Klipper
from pcb_assembly.hal.probe import ProbeSensor
from pcb_assembly.utils import get_class_module_path


class HeightTransformMeasurer:
    """プローブを用いて基板表面の高さを計測し、Z軸方向のTransformを返すクラス.

    KlipperのPROBEコマンドを実行し、ProbeSensorでZ座標を取得する。

    Example:
        probe = ProbeSensor(klipper.readonly)
        measurer = HeightTransformMeasurer(probe=probe, klipper=klipper)
        transform = measurer.measure()
        corrected = transform.apply(point)
    """

    def __init__(
        self,
        probe: ProbeSensor,
        klipper: Klipper,
        settle_time: float = 0.5,
    ) -> None:
        """HeightTransformMeasurerを初期化する.

        Args:
            probe: プローブセンサー
            klipper: Klipperクライアント
            settle_time: PROBE実行後の安定待機時間（秒）
        """
        self._probe = probe
        self._klipper = klipper
        self._settle_time = settle_time
        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def measure(self) -> Transform:
        """プローブで高さを計測し、Z方向のTransformを返す.

        Returns:
            Z軸方向の平行移動変換
        """
        self._logger.info("高さ計測を開始")
        self._klipper.send_gcode(
            gcode.GCode("PROBE") + gcode.wait(self._settle_time) + gcode.wait_for_done()
        )
        z = self._probe.get_last_z_result()
        self._logger.info(f"計測完了: z={z:.4f}mm")
        return Shift(z=z)
