"""基板からの高さを計測する."""

import logging

from pcb_assembly import gcode
from pcb_assembly.geometry import Transform, Translation
from pcb_assembly.hal import Klipper
from pcb_assembly.hal.probe import ProbeSensor
from pcb_assembly.utils import get_class_module_path


class HeightTransformMeasurer:
    """プローブを用いて基板表面の高さを計測し、Z軸方向のTransformを返すクラス.

    Klipperのプロービングマクロを実行し、ProbeSensorで変位を計測する。

    Example:
        measurer = HeightTransformMeasurer(probe=probe, klipper=klipper)
        transform = measurer.measure()
        corrected = transform.apply(point)
    """

    def __init__(
        self,
        probe: ProbeSensor,
        klipper: Klipper,
        macro_name: str = "PROBE",
        settle_time: float = 0.5,
    ) -> None:
        """HeightTransformMeasurerを初期化する.

        Args:
            probe: プローブセンサー
            klipper: Klipperクライアント
            macro_name: プロービング用のKlipperマクロ名
            settle_time: マクロ実行後の安定待機時間（秒）

        Raises:
            RuntimeError: マクロがKlipperに定義されていない場合
        """
        if not klipper.has_macro(macro_name):
            raise RuntimeError(f"マクロ '{macro_name}' がKlipperに定義されていません")

        self._probe = probe
        self._klipper = klipper
        self._macro_name = macro_name
        self._settle_time = settle_time
        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def measure(self) -> Transform:
        """プローブで高さを計測し、Z方向のTransformを返す.

        Returns:
            Z軸方向の平行移動変換
        """
        self._logger.info("高さ計測を開始")

        with self._probe:
            self._klipper.send_gcode(
                gcode.GCode(self._macro_name)
                + gcode.wait(self._settle_time)
                + gcode.wait_for_done()
            )

        result = self._probe.result()
        self._logger.info(f"計測完了: min={result.min:.4f}mm, max={result.max:.4f}mm")

        return Translation(z=result.min)
