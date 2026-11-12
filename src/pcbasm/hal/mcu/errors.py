"""MCU（Klipper のプロトコルで話すファーム）との通信の例外."""

from pcbasm.errors import DeviceError


class McuError(DeviceError):
    """MCU と話せない、または MCU から読めないデータが届いた."""


class McuShutdown(McuError):
    """MCU が shutdown した（``shutdown`` か ``is_shutdown`` を受けた）.

    ``reason`` は MCU が報告した理由（辞書の ``static_string_id`` の文字列）。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"MCU が shutdown した: {reason}")
        self.reason = reason
