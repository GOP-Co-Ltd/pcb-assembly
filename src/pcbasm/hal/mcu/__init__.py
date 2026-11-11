"""Klipper のプロトコルで話す MCU（pnp-motion ファーム）とのクライアント.

- :mod:`pcbasm.hal.mcu.wire`: CRC16、VLQ、メッセージブロックの組み立てと分解
- :mod:`pcbasm.hal.mcu.dictionary`: データ辞書と、メッセージの符号化・復号
- :mod:`pcbasm.hal.mcu.transport`: シリアル通信、ブロックの送り直し、問い合わせと購読
- :mod:`pcbasm.hal.mcu.clock`: MCU の clock とホストの時刻の対応の推定
- :mod:`pcbasm.hal.mcu.tmc2209`: TMC2209 のレジスタの値と UART の datagram
- :mod:`pcbasm.hal.mcu.errors`: 例外
- :mod:`pcbasm.hal.mcu.settings`: ステージの設定（hal の値型）
- :mod:`pcbasm.hal.mcu.firmware`: 設定の列、TMC2209 の設定、``velocity_stream`` の型付きの操作
- :mod:`pcbasm.hal.mcu.homing`: 1 軸の原点出し
- :mod:`pcbasm.hal.mcu.stage`: :class:`~pcbasm.hal.motion.MotionStage` の MCU 版

外から MCU を開く口は :func:`open_motion_stage` と :func:`send_emergency_stop` だけ（テストは root conftest の
``forbid_real_motion`` がこの 2 つを塞ぐ）。呼ぶ側はこのモジュールの属性として呼ぶ（``mcu.open_motion_stage(...)``）。

ファームの仕様（Protocol.md、MCU_Commands.md、C ソース）は GPLv3 だが、コードは写していない。
各モジュールの docstring に、仕様として読んだ出典を書く。
"""

from .clock import ClockEstimator, ClockSync, to_clock32
from .dictionary import DataDictionary, Message, ParamValue
from .errors import McuError, McuShutdown
from .firmware import send_emergency_stop
from .settings import AxisSettings, Kinematics, McuMotionSettings
from .stage import McuMotionStage, open_motion_stage
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
    # firmware
    "send_emergency_stop",
    # settings
    "AxisSettings",
    "Kinematics",
    "McuMotionSettings",
    # stage
    "McuMotionStage",
    "open_motion_stage",
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
