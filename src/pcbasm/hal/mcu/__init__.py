"""Klipper のプロトコルで話す MCU（pnp-motion ファーム）とのクライアント.

- :mod:`pcbasm.hal.mcu.wire`: CRC16、VLQ、メッセージブロックの組み立てと分解
- :mod:`pcbasm.hal.mcu.dictionary`: データ辞書と、メッセージの符号化・復号
- :mod:`pcbasm.hal.mcu.transport`: シリアル通信、ブロックの送り直し、問い合わせと購読
- :mod:`pcbasm.hal.mcu.clock`: MCU の clock とホストの時刻の対応の推定
- :mod:`pcbasm.hal.mcu.tmc2209`: TMC2209 のレジスタの値と UART の datagram
- :mod:`pcbasm.hal.mcu.errors`: 例外

ファームの仕様（Protocol.md、MCU_Commands.md、C ソース）は GPLv3 だが、コードは写していない。
各モジュールの docstring に、仕様として読んだ出典を書く。
"""

from .clock import ClockEstimator, ClockSync, to_clock32
from .dictionary import DataDictionary, Message, ParamValue
from .errors import McuError, McuShutdown
from .tmc2209 import (
    TmcSettings,
    current_bits,
    decode_read_reply,
    encode_uart,
    read_request,
    register_values,
    write_datagram,
)
from .transport import McuTransport

__all__ = [
    # clock
    "ClockEstimator",
    "ClockSync",
    "to_clock32",
    # dictionary
    "DataDictionary",
    "Message",
    "ParamValue",
    # errors
    "McuError",
    "McuShutdown",
    # tmc2209
    "TmcSettings",
    "current_bits",
    "decode_read_reply",
    "encode_uart",
    "read_request",
    "register_values",
    "write_datagram",
    # transport
    "McuTransport",
]
