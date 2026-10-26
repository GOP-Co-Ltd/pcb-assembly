"""例外の型.

web は型だけを見て HTTP ステータスへ写す（``web.api.errors``）。
"""


class PcbasmError(Exception):
    """``pcbasm`` が送出する例外の基底."""


class DeviceError(PcbasmError, RuntimeError):
    """装置が要求を受け付けなかった.

    RuntimeError の派生なので、既存の ``except RuntimeError`` はこれまでどおり捕まえる。
    """
