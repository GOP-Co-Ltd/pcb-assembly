"""Klipperの[probe]セクションを利用した電気接触式プローブセンサー."""

from .klipper import ReadonlyKlipper


class ProbeSensor:
    """Klipperの[probe]セクションを利用した電気接触式プローブセンサー.

    Example:
        klipper = Klipper()
        probe = ProbeSensor(klipper.readonly)
        # PROBE gcodeを実行後
        z = probe.get_last_z_result()
    """

    def __init__(self, klipper: ReadonlyKlipper) -> None:
        """ProbeSensorを初期化する.

        Args:
            klipper: Klipperクライアント

        Raises:
            RuntimeError: printer.cfgに[probe]セクションがない場合
        """
        config = klipper.get_config()
        if "probe" not in config:
            raise RuntimeError("printer.cfgに[probe]セクションを追加してください")
        self._klipper = klipper

    def get_last_z_result(self) -> float:
        """最後のプローブ計測のZ座標を返す.

        Returns:
            プローブが接触したZ座標 (mm)
        """
        return float(self._klipper.get_status("probe", "last_z_result"))
