"""[pnp] 節：Pick and Place（部品実装）.

``[pnp]`` があれば、その機体は部品を実装できる（:attr:`pcbasm.config.MachineConfig.pnp`）。
中身は PnP を実装するときに足す。いまは空の表だけを受け付ける。
"""

import attrs


@attrs.frozen
class PnpConfig:
    """部品実装の設定（[pnp]）."""
