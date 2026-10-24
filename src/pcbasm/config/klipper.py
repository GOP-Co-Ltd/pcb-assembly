"""[klipper] 節：Moonraker の接続先."""

import attrs


@attrs.frozen
class KlipperConfig:
    """Klipper の設定."""

    host: str = "localhost"
    port: int = 7125
