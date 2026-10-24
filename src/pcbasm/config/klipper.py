"""[klipper] 節：Moonraker の接続先."""

import attrs


@attrs.frozen
class KlipperConfig:
    """Klipper の設定（接続先に既定値は無い。書き忘れたまま本番の Moonraker へつながないように）."""

    host: str
    port: int
