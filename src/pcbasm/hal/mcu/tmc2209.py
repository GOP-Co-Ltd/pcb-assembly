"""TMC2209 のレジスタの値と、UART の datagram の組み立て・復号（純粋）.

ステッピングモーターのドライバー TMC2209 に、ファームの ``tmcuart_send`` を通して書くレジスタの値を
求め、送るビット列を作り、``tmcuart_response`` で返ってきたビット列を復号する。I/O は持たない。

仕様として読んだ出典:

- Trinamic TMC2209 Datasheet Rev 1.09: レジスタのアドレスとビット位置（5 章）、SLAVECONF の
  SENDDELAY（5.1 節。0 と 1 の 8 bit times は複数のドライバーで UART を共有するときに使えない）、
  UART の datagram と CRC8（4.1 節）、電流の式 I_RMS = (CS+1)/32 × V_FS/(R_SENSE+20 mΩ) × 1/√2 と V_FS（VSRTL 0.325 V、
  VSRTH 0.180 V）（9 章）、TSTEP と TPWMTHRS（1/256 microstep の間隔を f_CLK=12 MHz で数えた値。
  TSTEP ≧ TPWMTHRS のあいだ stealthChop）
- ファームの ``src/firmware/pnp-motion/klipper/src/tmcuart.c``: ``tmcuart_send`` は渡したバイト列を
  各バイトの LSB から 1 bit ずつそのまま線に出し、読みでは start bit から 1 bit ずつ LSB から詰めて返す。
  そのため start bit（0）と stop bit（1）はホストが付けて詰める。先頭 6 bit が ``0x2a`` のときは
  sync nibble で baud を合わせる
- ``src/firmware/pnp-motion/klipper/docs/Config_Reference.md`` の ``[tmc2208]`` の節（``[tmc2209]`` が
  参照する）: 設定項目 ``driver_*`` の既定値。CHOPCONF（toff・hstrt・hend・tbl）、IHOLDDELAY、
  TPOWERDOWN、PWMCONF（pwm_ofs・pwm_grad・pwm_freq・pwm_autoscale・pwm_autograd・pwm_reg・pwm_lim・
  freewheel）の定数はこれに合わせる。kurousagi の ``deploy/templates/kurousagi/printer.cfg`` は
  run_current と stealthchop_threshold だけを書き、ほかはこの既定値で動いていた。同じ値にすれば、
  Klipper で調子を確かめたモーターの動きを変えずに済む。データシートのリセット値（例: PWMCONF の
  0xC10D0024）ではなくこちらを使うのはそのため
"""

from __future__ import annotations

import math

import attrs

# レジスタのアドレス（データシート 5 章）
GCONF = 0x00
IFCNT = 0x02
SLAVECONF = 0x03
IHOLD_IRUN = 0x10
TPOWERDOWN = 0x11
TPWMTHRS = 0x13
TCOOLTHRS = 0x14
SGTHRS = 0x40
COOLCONF = 0x42
CHOPCONF = 0x6C
PWMCONF = 0x70

_F_CLK = 12_000_000  # 内部クロックの公称値 [Hz]
_V_FS_LOW_SENSE = 0.325  # vsense=0 の full scale の電圧 [V]
_V_FS_HIGH_SENSE = 0.180  # vsense=1 の full scale の電圧 [V]
_R_INTERNAL = 0.020  # 電流の式で sense 抵抗に足す 20 mΩ
_CS_MAX = 31
_TPWMTHRS_MAX = (1 << 20) - 1

# 送る datagram の 1 byte 目。下位 4 bit が sync（1010 を LSB から）、上位 4 bit は reserved（CRC には含む）。
# reserved を 1 にすると、start bit と合わせた先頭 6 bit が tmcuart.c の baud 合わせの条件 0x2a になる
_SYNC_TX = 0xF5
_WRITE_FLAG = 0x80
_READ_REPLY_BYTES = 8

# GCONF: pdn_disable（bit 6、PDN_UART を UART 専用にする）、mstep_reg_select（bit 7、microsteps を
# MRES で決める）、multistep_filt（bit 8）。en_SpreadCycle は bit 2
_GCONF_BASE = (1 << 6) | (1 << 7) | (1 << 8)
_GCONF_EN_SPREADCYCLE = 1 << 2

# CHOPCONF の固定の部分: toff=3（bit 0-3）、hstrt=5（bit 4-6）、hend=0（bit 7-10）、tbl=2（bit 15-16）、
# intpol=1（bit 28）、dedge=1（bit 29、step の両エッジで 1 microstep 進む）。
# dedge=1 なので、ファームの stepper は両エッジで step を出す設定（config_stepper の invert_step=-1）にする
_CHOPCONF_BASE = 3 | (5 << 4) | (0 << 7) | (2 << 15) | (1 << 28) | (1 << 29)
_CHOPCONF_VSENSE_SHIFT = 17
_CHOPCONF_MRES_SHIFT = 24

_IHOLDDELAY = 8  # IHOLD_IRUN の bit 16-19
_TPOWERDOWN_VALUE = 20  # 停止から hold current へ下げるまで

# SLAVECONF: senddelay=2（bit 8-11）。読み出しの応答を 3 × 8 bit times 待ってから返させる。
# kurousagi は 3 つのドライバーが 1 本の UART を共有するので、0 と 1 は使えない（データシート 5.1 節）
_SLAVECONF_VALUE = 2 << 8

# PWMCONF: pwm_ofs=36（bit 0-7）、pwm_grad=14（bit 8-15）、pwm_freq=1（bit 16-17）、
# pwm_autoscale=1（bit 18）、pwm_autograd=1（bit 19）、freewheel=0（bit 20-21）、pwm_reg=8（bit 24-27）、
# pwm_lim=12（bit 28-31）
_PWMCONF_VALUE = (
    36 | (14 << 8) | (1 << 16) | (1 << 18) | (1 << 19) | (8 << 24) | (12 << 28)
)


@attrs.frozen
class TmcSettings:
    """1 軸の TMC2209 の設定.

    電流は RMS [A]。``hold_current`` が None なら ``run_current`` と同じにする。
    ``stealthchop_threshold`` [mm/s] より遅いあいだ stealthChop で、0 なら常に spreadCycle。
    """

    run_current: float
    hold_current: float | None = None
    sense_resistor: float = 0.110
    microsteps: int = 64
    stealthchop_threshold: float = 999999.0


def current_bits(
    run_current: float, hold_current: float, sense_resistor: float
) -> tuple[int, int, bool]:
    """電流 [A RMS] から (irun, ihold, vsense) を求める.

    分解能が細かい vsense=1 を先に試し、大きいほうの電流で CS が 31 を超えるなら vsense=0 にする。 CS
    は式の値を四捨五入する。vsense=0 でも 31 を超える電流は ValueError。
    """
    peak = max(run_current, hold_current)
    for vsense, v_fs in ((True, _V_FS_HIGH_SENSE), (False, _V_FS_LOW_SENSE)):
        if _current_scale(peak, sense_resistor, v_fs) <= _CS_MAX:
            return (
                _current_scale(run_current, sense_resistor, v_fs),
                _current_scale(hold_current, sense_resistor, v_fs),
                vsense,
            )
    raise ValueError(
        f"電流 {peak} A は sense_resistor {sense_resistor} Ω の full scale を超える"
    )


def _current_scale(current: float, sense_resistor: float, v_fs: float) -> int:
    cs = 32 * math.sqrt(2) * current * (sense_resistor + _R_INTERNAL) / v_fs - 1
    return max(0, math.floor(cs + 0.5))


def register_values(settings: TmcSettings, step_distance: float) -> dict[int, int]:
    """書くレジスタと値を、書く順に並べて返す.

    ``step_distance`` は 1 microstep で進む距離 [mm]（TPWMTHRS を求めるのに使う）。
    GCONF を最初に書く（pdn_disable と mstep_reg_select が入るまで、UART の電流と MRES が効かない）。
    SLAVECONF は書き込み専用なので、読み戻して確かめられない。
    microsteps が 1〜256 の 2 のべきでなければ ValueError。
    """
    mres = _mres(settings.microsteps)
    hold = (
        settings.run_current if settings.hold_current is None else settings.hold_current
    )
    irun, ihold, vsense = current_bits(
        settings.run_current, hold, settings.sense_resistor
    )
    stealthchop = settings.stealthchop_threshold > 0
    return {
        GCONF: _GCONF_BASE | (0 if stealthchop else _GCONF_EN_SPREADCYCLE),
        SLAVECONF: _SLAVECONF_VALUE,
        CHOPCONF: _CHOPCONF_BASE
        | (int(vsense) << _CHOPCONF_VSENSE_SHIFT)
        | (mres << _CHOPCONF_MRES_SHIFT),
        IHOLD_IRUN: ihold | (irun << 8) | (_IHOLDDELAY << 16),
        TPOWERDOWN: _TPOWERDOWN_VALUE,
        TPWMTHRS: _tpwmthrs(settings, step_distance) if stealthchop else 0,
        TCOOLTHRS: 0,
        SGTHRS: 0,
        COOLCONF: 0,
        PWMCONF: _PWMCONF_VALUE,
    }


def _mres(microsteps: int) -> int:
    if microsteps < 1 or microsteps > 256 or microsteps & (microsteps - 1):
        raise ValueError(f"microsteps は 1〜256 の 2 のべき: {microsteps}")
    return 8 - microsteps.bit_length() + 1


def _tpwmthrs(settings: TmcSettings, step_distance: float) -> int:
    # 速度 v [mm/s] のとき microstep は v / step_distance [Hz]。1/256 microstep の間隔を f_CLK で数えると
    # TSTEP = f_CLK × microsteps × step_distance / (256 × v)
    tstep = (
        _F_CLK
        * settings.microsteps
        * step_distance
        / (256 * settings.stealthchop_threshold)
    )
    return min(_TPWMTHRS_MAX, math.floor(tstep + 0.5))


def crc8(data: bytes) -> int:
    """Datagram の CRC8（多項式 x^8 + x^2 + x + 1、各バイトを LSB から流す。データシート 4.1.2 節）."""
    crc = 0
    for byte in data:
        for i in range(8):
            if (crc >> 7) ^ ((byte >> i) & 1):
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def write_datagram(address: int, register: int, value: int) -> bytes:
    """レジスタへの書き込みの datagram（8 byte。値は MSB から）."""
    body = bytes([_SYNC_TX, address, register | _WRITE_FLAG]) + value.to_bytes(4, "big")
    return body + bytes([crc8(body)])


def read_request(address: int, register: int) -> bytes:
    """レジスタの読み出し要求の datagram（4 byte）."""
    body = bytes([_SYNC_TX, address, register])
    return body + bytes([crc8(body)])


def encode_uart(datagram: bytes) -> bytes:
    """各バイトに start bit（0）と stop bit（1）を付け、LSB から詰める.

    ファームの tmcuart_send にそのまま渡す形になる。最後のバイトの余った bit は 1（線の待機と同じ）で埋める。
    """
    bits: list[int] = []
    for byte in datagram:
        bits.append(0)
        bits.extend((byte >> i) & 1 for i in range(8))
        bits.append(1)
    bits.extend([1] * (-len(bits) % 8))
    return bytes(
        sum(bit << i for i, bit in enumerate(bits[k : k + 8]))
        for k in range(0, len(bits), 8)
    )


def decode_read_reply(data: bytes) -> tuple[int, int] | None:
    """tmcuart_response で受け取ったビット列から (register, value) を取り出す.

    長さ、start bit と stop bit、CRC のどれかが合わなければ None（応答がない、線が乱れたなど）。
    """
    if len(data) * 8 < _READ_REPLY_BYTES * 10:
        return None
    bits = [(data[i >> 3] >> (i & 7)) & 1 for i in range(_READ_REPLY_BYTES * 10)]
    reply = bytearray()
    for k in range(0, len(bits), 10):
        frame = bits[k : k + 10]
        if frame[0] != 0 or frame[9] != 1:
            return None
        reply.append(sum(bit << i for i, bit in enumerate(frame[1:9])))
    if crc8(bytes(reply[:-1])) != reply[-1]:
        return None
    return reply[2], int.from_bytes(reply[3:7], "big")
