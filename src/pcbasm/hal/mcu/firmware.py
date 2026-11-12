"""Pnp-motion ファームへの命令: 設定の列、TMC2209 の設定、``velocity_stream`` の型付きの操作.

仕様として読んだもの（コードは写していない。pcbasm は MIT、出典は GPLv3）:

- ``src/firmware/pnp-motion/README.md`` の「コマンド」と「ホストが守る前提」（設定の順序、範囲、期限、
  開始の clock は今から ``lead_ticks`` 以上先）
- ``src/firmware/pnp-motion/module/velocity_stream.c``（状態の遷移。``velocity_stream_stop`` は ``stream`` の
  ときだけ ``stopping`` にし、積んだステップが出尽くしてから ``idle`` にする。動いている間の
  ``set_limits``・``set_position``・``start`` は shutdown。期限が切れても ``stream`` のまま速度 0 で流れ続ける）
- ファームの C ソース ``klipper/src`` の ``basecmd.c``（``allocate_oids``、``get_config`` と ``finalize_config`` の
  crc）、``stepper.c``（``config_stepper`` の ``invert_step=-1`` は両エッジで step を出す）、``endstop.c``
  （``endstop_home`` の ``sample_count=0`` で外す）、``trsync.c``（``trsync_start`` で登録を消してから張り直す）、
  ``gpiocmds.c``（``config_digital_out`` の ``value`` は設定したときの値、``default_value`` は shutdown のときの値、
  ``max_duration=0`` は期限なし）、``tmcuart.c``、``sensor_hx71x.c``（``config_hx71x`` と ``query_hx71x``。読み取りは
  :mod:`pcbasm.hal.mcu.load_cell`。``hx71x_attach_trigger_analog``。誤りのサンプルでは ``trigger_analog_update`` を
  呼ばない）、``sos_filter.c``（``config_sos_filter``、``sos_filter_set_offset_scale``。``(値 + offset) × scale`` を
  ``scale_frac_bits`` だけ右へずらし、断面が 0 個ならそれが出力。あふれたら誤り）、``trigger_analog.c``
  （``config_trigger_analog``、``trigger_analog_set_raw_range``、``trigger_analog_set_trigger``、``trigger_analog_home``。
  下の「接触停止」）
- TMC2209 Datasheet Rev 1.09 の「IFCNT」（UART の書き込みが正しく届くたびに 1 進む 8 bit の数）

``tools/bench.py``（GPLv3）は、ホストがどのコマンドをどの順に送るかを確かめるためにだけ読んだ。
名前・構造・コメント・式の形は写していない。

決めたこと:

- **oid:** ``velocity_stream`` が 0、stepper が 1〜3、endstop が 4〜6、enable が 7〜9、trsync が 10、tmcuart が 11、
  LED が 12、hx71x が 13、sos_filter が 14、trigger_analog が 15。LED とロードセルが無い機体でも 16 個を割り当てる
  （使わない oid は空のまま）。oid の数を変えると crc も変わり、その版で最初に接続したときに 1 回 reset する
- **LED:** 設定の列で ``config_digital_out`` の ``value`` と ``default_value`` を 1 にする（設定したときから点け、
  shutdown しても消さない）。ホストから点け消しはしない。MCU を reset すると（再起動、設定が変わったとき、
  この版で最初に接続したとき）、設定し直すまでの間はピンが既定の状態に戻って消える（ファームの外の話）
- **ロードセル:** 接続の最後（設定し直したときも、使い回したときも）に ``query_hx71x`` で読み始め、:meth:`close`
  で止める。Z の速度が 0 でない指令を送るたびに、自動の風袋引きを 1 s 止める
- **step:** TMC2209 を dedge=1（両エッジで 1 microstep）で書くので、stepper は両エッジで step を出す
  （``invert_step=-1``、``step_pulse_ticks=0``）
- **crc:** ``finalize_config`` には、設定の列を符号化したバイト列の CRC32 を入れる。接続したとき、MCU が
  同じ crc で設定済みで shutdown していなければ reset せずに使い回す（reset で励磁が切れると Z が落ちうる）。
  使い回すときは、前のプロセスが残したものを片付ける: ``velocity_stream_stop`` → ``idle`` を待つ →
  endstop を外す → 原点出しの前の可動域に戻し、位置を 0 にする。原点は「未」になる
- **開始の時刻:** ``velocity_stream_start`` と ``endstop_home`` の clock は、今から 50 ms（と先読み）先に置く。
  時刻の推定の誤差と送る遅れで ``lead_ticks`` を割ると shutdown するため
- **問い合わせ:** 応答を待つのは、実行し直しても結果の変わらないコマンド（``get_config``・
  ``velocity_stream_query``・``endstop_query_state``・``tmcuart_send``）だけ。``velocity_stream_query`` の前には
  ``get_config`` の応答を待つ（:meth:`PnpMotionFirmware.query`）
- **TMC2209 の確認:** レジスタを 1 つ書くたびに IFCNT を読み、1 つだけ進んだことを確かめる。書き込みが
  届かなかったときも、応答の欠落で 2 度実行されたときも、数が合わずに失敗として現れる
- **座標指定の移動（:meth:`PnpMotionFirmware.travel`）:** ファームは位置の指令を持たないので、可動域の端で
  減速して止まる性質を使う。動かす軸の可動域を「いまの位置〜行き先」の側だけ行き先で切り詰めて流し、
  着いたら止めてから元の可動域に戻す（流している間の ``set_limits`` は shutdown）。行き先までの差が
  ``_ARRIVAL_TOLERANCE`` 以下の軸は着いているとみなして動かさない。corexy では止まった位置が 1 ステップ
  ずれて報告されることがあり（velocity_stream.c の開始の検査も 1 ステップの余裕をみる）、その差を
  動かそうとすると切り詰めた可動域が空（``min_pos >= max_pos``）になって shutdown するため

接触停止（ロードセルがある機体）:

- **経路:** MCU の中で完結させる。``hx71x`` のサンプル → ``trigger_analog``（``sos_filter`` で
  ``(値 − 風袋) × 符号`` にする）→ 原点出しと同じ trsync（oid 10）→ 3 軸の stepper を即座に止め、``velocity_stream``
  を ``contact`` にする。ホストを待たないので、止まるまでの遅れはサンプルの周期（80 SPS で 12.5 ms）の 1〜2 個分
- **設定の列:** ``config_sos_filter``（断面 0 個。フィルタは掛けない: 80 SPS では遅れが増えるので、受け入れ試験
  A34・A35 の実測を見て要れば足す）→ ``config_trigger_analog`` → ``trigger_analog_set_raw_range``（HX711 が
  張り付いた ±2^23 の端を誤りにする。oid は 0 で埋まるので、送らないと範囲が 0〜0 になり全サンプルが誤りになる）→
  ``trigger_analog_set_trigger``（``gt`` で ``round(trigger_force × counts_per_gram)`` を超えたら。押される向き
  だけを見る）→ ``hx71x_attach_trigger_analog``
- **風袋:** :class:`~pcbasm.hal.mcu.load_cell.LoadCell` が風袋を変えるたびに ``sos_filter_set_offset_scale`` で
  ``offset = −風袋``、``scale = ±1``（``inverted`` で −1）を送る。設定したばかりの scale は 0 で出力も 0 なので、
  風袋を取るまでは張らない（張ろうとしたら :class:`LoadCellFault`）
- **trsync の理由（reason）:**

  ====  ==============================================================
  0     期限切れ（``trsync_start`` の ``expire_reason``。期限を張らないので来ない）
  1〜3  X・Y・Z のリミットスイッチ（``endstop_home`` の ``trigger_reason``）
  4     接触（``trigger_analog_home`` の ``trigger_reason``）
  5〜8  ロードセルの異常。``trigger_analog_home`` の ``error_reason`` を 5 にし、ファームが
        ``RAW_RANGE`` 0・``OVERFLOW`` 1・``MONITOR`` 2・``SENSOR_SPECIFIC`` 3 を足す
  ====  ==============================================================

  そのほかの理由（0 や 9 以上）も、黙って通さないようロードセルの異常として扱う

- **見張り:** ``monitor_ticks`` を 1 サンプルの周期、``monitor_max`` を 3 にする。サンプルが来るたびに数えるのを
  0 に戻し、周期ごとに 1 進め、3 を超えたら ``MONITOR``。約 5 周期（80 SPS で約 60 ms）サンプルが来なければ
  異常で止まる。hx71x は誤りのサンプル（DESYNC、READ_TOO_LONG）を ``trigger_analog`` に渡さないので、
  誤りが続いても ``MONITOR`` になる（:class:`~pcbasm.hal.mcu.load_cell.LoadCell` が読み直して、それより早く
  戻れば止まらない）
- **張る・外す:** 張るのは :meth:`PnpMotionFirmware.start` の直前だけ（止まっているとき）。``trsync_start`` が
  前の登録を消すので、二重に登録しない。外すのは :meth:`PnpMotionFirmware.stop` の後、原点出しの後
  （:meth:`PnpMotionFirmware.disarm`）、接触保持に入るとき（:meth:`PnpMotionFirmware.clear_trigger`）、
  使い回して接続したとき。``trigger_analog_home`` の ``monitor_ticks=0`` で外す
- **報告:** 接触や異常で止まると、``velocity_stream_state`` の ``contact`` と ``trsync_state`` の理由が別々に届く。
  ``contact`` を受けたら流していないとする（受信スレッド。スティックで流している間はホストが問い合わせない
  ので、そのままだと止まったのに速度を送り続ける）。``contact`` は次の ``start`` まで変わらず、それより前の報告は
  :meth:`PnpMotionFirmware.query` の ``get_config`` で掃き出すので、古い ``contact`` が新しい ``start`` の後に来ない
"""

from __future__ import annotations

import contextlib
import enum
import logging
import math
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import attrs

from pcbasm.hal.mcu.clock import ClockSync, to_clock32
from pcbasm.hal.mcu.dictionary import Message, ParamValue
from pcbasm.hal.mcu.errors import McuError
from pcbasm.hal.mcu.load_cell import LoadCell
from pcbasm.hal.mcu.settings import AxisSettings, McuMotionSettings
from pcbasm.hal.mcu.tmc2209 import (
    IFCNT,
    decode_read_reply,
    encode_uart,
    read_request,
    register_values,
    write_datagram,
)
from pcbasm.hal.mcu.transport import McuTransport
from pcbasm.hal.motion import Axis

_logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
_NOT_PNP_MOTION = "pnp-motion ファームではありません。`./install.sh firmware pnp-motion` で書き込んでください"
_REQUIRED_COMMANDS = (
    "config_velocity_stream",
    "velocity_stream_set_limits",
    "velocity_stream_set_position",
    "velocity_stream_start",
    "velocity_stream_set_velocity",
    "velocity_stream_stop",
    "velocity_stream_stop_on_trigger",
    "velocity_stream_query",
)

AXES: tuple[Axis, ...] = ("x", "y", "z")

# oid
_STREAM = 0
_STEPPERS = (1, 2, 3)
_ENDSTOPS = (4, 5, 6)
_ENABLES = (7, 8, 9)
_TRSYNC = 10
_TMCUART = 11
_LED = 12
_HX71X = 13
_SOS_FILTER = 14
_TRIGGER_ANALOG = 15
_OID_COUNT = 16

# endstop_home の trigger_reason。trsync_state がこの値を返すので、止めたスイッチの軸が分かる
_SWITCH_REASON: dict[Axis, int] = {
    axis: 1 + index for index, axis in enumerate(("x", "y", "z"))
}

# trigger_analog_home の trigger_reason と error_reason（モジュールの docstring の「接触停止」）
_CONTACT_REASON = 4
_SENSOR_ERROR_REASON = 5
# trigger_analog.c の TE_* の順（error_reason に足す値）
_SENSOR_ERRORS = ("RAW_RANGE", "OVERFLOW", "MONITOR", "SENSOR_SPECIFIC")
# HX711 は 24 bit。張り付いた端（0x7FFFFF と 0x800000）を範囲の外にする
_RAW_MIN = -(1 << 23) + 1
_RAW_MAX = (1 << 23) - 2
_MONITOR_MAX = 3
# 接触や異常で止まったとき、trsync_state と contact の報告の片方だけが届いた状態から、もう片方を待つ長さ [s]
_TRIGGER_WAIT = 0.5

# 時刻を指定するコマンド（velocity_stream_start、endstop_home）を、今からどれだけ先に置くか [s]。
# 時刻の推定の誤差と送る遅れを吸収する（ファームは lead_ticks より近い開始を shutdown にする）
_SCHEDULE_AHEAD = 0.05
_POLL = 0.02  # 状態を問い合わせる間隔 [s]
_STOP_MARGIN = 2.0  # 止まるのを待つ長さ = 減速にかかる時間 + これ [s]
# 原点出しの前の可動域は、いまの位置から前後に可動域の長さのこの倍。可動域の中のどこにいてもスイッチに
# 届き、可動域の外へ手で押し出されていても同じ長さまでは探せる
_SEARCH_SPAN = 2.0
# 座標指定の移動で、行き先に着いたとみなす差 [steps]（モジュールの docstring）
_ARRIVAL_TOLERANCE = 1
_TRAVEL_SLACK = 1.0  # 座標指定の移動を待つ長さの余裕 [s]
# endstop の見方: rest ごとにピンを見て、合えば sample の間隔で count 回続けて合えば引く
_ENDSTOP_REST = 0.0005  # [s]
_ENDSTOP_SAMPLE = 0.000025  # [s]
_ENDSTOP_SAMPLE_COUNT = 4
# TMC2209 の読み出しを試す回数。単線の UART はまれに 1 回乱れる（CRC の不一致）ので、続けて乱れるまで待つ。
# 電源が無いときは毎回応えないので、回数を増やしても待つ時間が延びるだけ
_TMC_READ_ATTEMPTS = 3
_TMC_REPLY_BYTES = 10  # 応答の 8 byte に start と stop の bit を付けた 80 bit
_RESET_SETTLE = 0.5  # reset のあと、USB が付き直すのを待ち始めるまで [s]
_RECONNECT_TIMEOUT = 10.0  # reset のあと開き直せるまで待つ長さ [s]
_RECONNECT_RETRY = 0.2  # [s]


class ProcedureError(McuError):
    """手順が終わらなかった（スイッチに届かない、行き先に着かない、止まらない、TMC2209 が応えない）.

    MCU との接続は生きている。
    """


class ContactStop(ProcedureError):
    """ロードセルの接触で止まった（trsync の理由が接触）."""


class LoadCellFault(ProcedureError):
    """ロードセルの異常で止まった、または読めないので接触停止を張れない."""


class StreamState(enum.Enum):
    """``velocity_stream`` の状態（辞書の列挙 ``velocity_stream_state`` の名前）."""

    IDLE = "idle"
    STREAM = "stream"
    STOPPING = "stopping"
    CONTACT = "contact"

    @property
    def running(self) -> bool:
        """流している（``set_limits``・``set_position``・``start`` を送れない）."""
        return self in (StreamState.STREAM, StreamState.STOPPING)


@attrs.frozen
class StreamReport:
    """``velocity_stream_state`` の報告.

    Attributes:
        clock: 報告した時刻（64 bit の MCU の clock）
        seq: 最後に受けた速度指令の連番
        state: 状態
        position: X・Y・Z の位置 [steps]
        velocity: X・Y・Z の計画の速度 [steps/s]
    """

    clock: int
    seq: int
    state: StreamState
    position: tuple[int, int, int]
    velocity: tuple[int, int, int]


@attrs.frozen
class _ConfigState:
    is_config: bool
    crc: int
    is_shutdown: bool


class PnpMotionFirmware:
    """設定を済ませた pnp-motion ファームへの命令.

    :func:`open_firmware` が、開いて identify した ``transport`` を渡して作る。作るときに時刻の同期を始め、
    設定するか前の設定を使い回して、原点が「未」の止まった状態にする。
    位置と速度はステップで扱う（mm との換算は呼び出し側）。
    :meth:`emergency_stop` と :attr:`latest`・:attr:`shutdown_reason` はどのスレッドからでも呼べる。
    ほかの操作は、呼び出し側が 1 つずつ呼ぶ。
    """

    def __init__(self, transport: McuTransport, settings: McuMotionSettings) -> None:
        self._transport = transport
        self._settings = settings
        frequency = transport.dictionary.constant("CLOCK_FREQ")
        if not isinstance(frequency, int):
            raise McuError(f"CLOCK_FREQ が整数ではない: {frequency!r}")
        self._clock = ClockSync(transport, frequency)
        self._seq = 0
        self._streaming = False
        self._reconfigured = False
        self._latest: StreamReport | None = None
        self._shutdown_reason: str | None = None
        self._bounds: dict[Axis, tuple[int, int]] = {}
        self._trigger_reason: int | None = None
        self._armed = False
        self._contact_armed = False
        self._load_cell = (
            None
            if settings.load_cell is None
            else LoadCell(
                transport,
                settings.load_cell,
                oid=_HX71X,
                clock_freq=frequency,
                on_tare=self._send_tare,
            )
        )
        transport.subscribe("velocity_stream_state", self._on_state, oid=_STREAM)
        transport.subscribe("trsync_state", self._on_trsync, oid=_TRSYNC)
        transport.subscribe("shutdown", self._on_shutdown)
        transport.subscribe("is_shutdown", self._on_shutdown)
        try:
            self._connect()
        except BaseException:
            self.close()
            raise

    @property
    def settings(self) -> McuMotionSettings:
        """設定."""
        return self._settings

    @property
    def reconfigured(self) -> bool:
        """接続したときに設定し直したか（False なら、前の設定を使い回した）."""
        return self._reconfigured

    @property
    def streaming(self) -> bool:
        """流しているか（:meth:`start` から :meth:`stop` か接触停止まで）."""
        return self._streaming

    def bounds(self, axis: Axis) -> tuple[int, int]:
        """軸の今の可動域 [steps]（最後に送った :meth:`set_limits` の値）."""
        return self._bounds[axis]

    @property
    def latest(self) -> StreamReport | None:
        """最後に受けた報告（定期の報告か、問い合わせの応答）."""
        return self._latest

    @property
    def load_cell(self) -> LoadCell | None:
        """ロードセル（設定に無ければ None）."""
        return self._load_cell

    @property
    def shutdown_reason(self) -> str | None:
        """MCU が shutdown した理由（shutdown していなければ None）."""
        return self._shutdown_reason

    def axis(self, axis: Axis) -> AxisSettings:
        """軸の設定."""
        return getattr(self._settings, axis)

    def search_limits(self, axis: Axis) -> tuple[int, int]:
        """原点出しの前の可動域 [steps]: いまの位置を 0 として、スイッチを探す距離だけ前後に."""
        settings = self.axis(axis)
        span = settings.to_steps(
            _SEARCH_SPAN * (settings.position_max - settings.position_min)
        )
        return -span, span

    def homed_limits(self, axis: Axis) -> tuple[int, int]:
        """原点出しの後の可動域 [steps]."""
        settings = self.axis(axis)
        return settings.to_steps(settings.position_min), settings.to_steps(
            settings.position_max
        )

    def set_enabled(self, enabled: bool) -> None:
        """3 軸のモーターの励磁を入れる・切る."""
        for oid, settings in zip(_ENABLES, self._settings.axes, strict=True):
            self._send(
                "update_digital_out", oid=oid, value=_enable_level(settings, enabled)
            )

    def set_limits(self, axis: Axis, low: int, high: int) -> None:
        """軸の可動域 [steps] を送る（速度と加速度の上限は設定の値）。止まっているときだけ送る.

        Raises:
            ValueError: ``low`` が ``high`` 以上（送ると MCU が shutdown するので送らない）
        """
        if low >= high:
            raise ValueError(f"{axis.upper()} の可動域が空になる: {low}〜{high} steps")
        settings = self.axis(axis)
        self._bounds[axis] = (low, high)
        self._send(
            "velocity_stream_set_limits",
            oid=_STREAM,
            axis=AXES.index(axis),
            min_pos=low,
            max_pos=high,
            max_velocity=settings.to_steps(settings.max_velocity),
            accel=settings.to_steps(settings.max_accel),
        )

    def set_position(self, axis: Axis, steps: int) -> None:
        """いまの位置を ``steps`` とする。止まっているときだけ、可動域の中の値を送る."""
        self._send(
            "velocity_stream_set_position",
            oid=_STREAM,
            axis=AXES.index(axis),
            pos=steps,
        )

    def forget_origin(self, *axes: Axis) -> None:
        """原点を「未」にする: 原点出しの前の可動域に広げ、いまの位置を 0 にする."""
        for axis in axes:
            self.set_limits(axis, *self.search_limits(axis))
        for axis in axes:
            self.set_position(axis, 0)

    def start(self, *, switch: Axis | None = None, contact: bool = False) -> None:
        """流し始める（速度は 0 から）。止まっているか、接触停止の後に送る.

        ``switch`` か ``contact`` があれば、先に同じ trsync に 3 軸の stepper と ``velocity_stream`` を登録し直し
        （``trsync_start`` が前の登録を消す）、``switch`` の軸のリミットスイッチと、``contact`` ならロードセルを張る。
        見始めるのは今から ``_SCHEDULE_AHEAD`` 先（流し始めるより前）。

        Args:
            switch: 張るリミットスイッチの軸
            contact: ロードセルの接触停止を張る

        Raises:
            LoadCellFault: ``contact`` だが、ロードセルが無いか、風袋がまだか、読めない
        """
        if switch is not None or contact:
            self._arm(switch, contact=contact)
        settings = self._settings
        clock = self._clock.now() + self._clock.ticks(
            settings.stream_lead + _SCHEDULE_AHEAD
        )
        self._send(
            "velocity_stream_start",
            oid=_STREAM,
            clock=to_clock32(clock),
            period_ticks=self._clock.ticks(settings.stream_period),
            lead_ticks=self._clock.ticks(settings.stream_lead),
            report_ticks=self._clock.ticks(settings.report_interval),
            ttl_ticks=self._clock.ticks(settings.stream_ttl),
        )
        self._streaming = True

    def set_velocity(self, velocity: tuple[int, int, int]) -> None:
        """速度 [steps/s] を、今から ``stream_ttl`` の期限付きで送る."""
        self._seq = (self._seq + 1) & 0xFFFF
        expire = self._clock.now() + self._clock.ticks(self._settings.stream_ttl)
        vx, vy, vz = velocity
        if vz and self._load_cell is not None:
            self._load_cell.note_z_motion()
        self._send(
            "velocity_stream_set_velocity",
            oid=_STREAM,
            seq=self._seq,
            vx=vx,
            vy=vy,
            vz=vz,
            expire_clock=to_clock32(expire),
        )

    def stop(self) -> None:
        """減速して止め、止まるまで待つ（``idle`` か、接触停止の ``contact``）.

        Raises:
            ProcedureError: 止まらない
        """
        self._send("velocity_stream_stop", oid=_STREAM)
        timeout = _STOP_MARGIN + max(
            settings.max_velocity / settings.max_accel
            for settings in self._settings.axes
        )
        deadline = time.monotonic() + timeout
        while self.query().state.running:
            if time.monotonic() > deadline:
                raise ProcedureError(f"{timeout:.1f} 秒待っても止まらない")
            time.sleep(_POLL)
        if self._contact_armed:
            self._disarm_contact()

    def query(self) -> StreamReport:
        """今の状態を問い合わせる（それまでに送ったコマンドを MCU が実行した後の状態）.

        ``velocity_stream_state`` は定期の報告と接触停止の報告でも届くので、問い合わせの応答と見分けられない。
        問い合わせの待ちに先に届いた報告が入ると、その問い合わせの本当の応答は宙に浮き、次の問い合わせが
        それを古いまま受けてしまう（``set_position`` の前の位置など）。そこで先に ``get_config`` の応答を待つ:
        MCU は受けた順に実行して応答も順に届くので、その後に届く ``velocity_stream_state`` は、それまでに
        送ったコマンドを実行した後のものになる。
        """
        self._transport.query("get_config", response="config")
        message = self._transport.query(
            "velocity_stream_query", response="velocity_stream_state", oid=_STREAM
        )
        report = self._report(message)
        self._latest = report
        self._streaming = report.state.running
        return report

    def drive(
        self,
        velocity: tuple[int, int, int],
        until: Callable[[StreamReport], bool],
        timeout: float,
    ) -> StreamReport | None:
        """流している間、速度を期限が切れる前に送り直しながら、報告が ``until`` を満たすまで待つ.

        ``contact`` で止まったら、``until`` を見る前に trsync の理由を調べる（理由がまだ届いていなければ待ち続ける）。

        Returns:
            ``until`` を満たした報告。``timeout`` 秒を過ぎたら None

        Raises:
            ContactStop: ロードセルの接触で止まった
            LoadCellFault: ロードセルの異常で止まった
        """
        deadline = time.monotonic() + timeout
        resend_at = 0.0
        while True:
            now = time.monotonic()
            if now >= resend_at:
                self.set_velocity(velocity)
                resend_at = now + self._settings.stream_ttl / 4
            report = self.query()
            if report.state is StreamState.CONTACT:
                self._raise_load_cell_stop(self._trigger_reason)
            if until(report):
                return report
            if now > deadline:
                return None
            time.sleep(_POLL)

    def travel(
        self,
        targets: Mapping[Axis, int],
        speed: float | None = None,
        *,
        contact: bool = False,
    ) -> None:
        """``targets`` の軸を行き先 [steps] へ直線に動かし、止まるまで待つ（止まっているときに呼ぶ）.

        進め方はモジュールの docstring の「座標指定の移動」。``speed`` [mm/s] は直線の速さで、省けば
        どの軸も ``max_velocity`` を超えない最も速い動き。行き先は、いまの可動域の中に置く。
        ``contact`` なら、ロードセルの接触停止を張って動かす。
        失敗しても、shutdown していなければ止めてから可動域を戻す。

        Raises:
            ProcedureError: 待っても行き先に着かない
            ContactStop: ロードセルの接触で止まった
            LoadCellFault: ロードセルの異常で止まった、または読めないので張れない
        """
        here = self.query().position
        offsets: dict[Axis, int] = {
            axis: target - here[AXES.index(axis)]
            for axis, target in targets.items()
            if abs(target - here[AXES.index(axis)]) > _ARRIVAL_TOLERANCE
        }
        if not offsets:
            return
        duration = self._travel_duration(offsets, speed)
        velocity = [0, 0, 0]
        for axis, offset in offsets.items():
            # 0 に丸まるほど遅い軸も、向きを保って 1 steps/s で動かす（端で止まる）
            velocity[AXES.index(axis)] = round(offset / duration) or (
                1 if offset > 0 else -1
            )
        ramp = max(
            self.axis(axis).to_mm(abs(velocity[AXES.index(axis)]))
            / self.axis(axis).max_accel
            for axis in offsets
        )
        # 台形の速度で距離を進むには、一定の速さのときより加速か減速の 1 回分だけ長くかかる
        timeout = duration + ramp + _TRAVEL_SLACK
        destinations: dict[Axis, int] = {axis: targets[axis] for axis in offsets}
        with self._narrowed(destinations, offsets):
            self.start(contact=contact)

            def settled(report: StreamReport) -> bool:
                return all(
                    report.velocity[AXES.index(axis)] == 0
                    and abs(report.position[AXES.index(axis)] - target)
                    <= _ARRIVAL_TOLERANCE
                    for axis, target in destinations.items()
                )

            if (
                self.drive((velocity[0], velocity[1], velocity[2]), settled, timeout)
                is None
            ):
                raise ProcedureError(f"{timeout:.1f} 秒たっても行き先に着かない")

    def _travel_duration(
        self, offsets: Mapping[Axis, int], speed: float | None
    ) -> float:
        """どの軸も同じ時間で動き終えるときの、その時間 [s]（直線に動く）."""
        distances: dict[Axis, float] = {
            axis: self.axis(axis).to_mm(offset) for axis, offset in offsets.items()
        }
        # いちばん時間のかかる軸が max_velocity で動く時間
        duration = max(
            abs(distance) / self.axis(axis).max_velocity
            for axis, distance in distances.items()
        )
        if speed is not None:
            duration = max(duration, math.hypot(*distances.values()) / speed)
        return duration

    @contextlib.contextmanager
    def _narrowed(
        self, destinations: Mapping[Axis, int], offsets: Mapping[Axis, int]
    ) -> Iterator[None]:
        """動かす軸の可動域の、向かう側を行き先で切り詰める。抜けるときに止めてから元に戻す."""
        saved: dict[Axis, tuple[int, int]] = {
            axis: self._bounds[axis] for axis in destinations
        }
        try:
            for axis, destination in destinations.items():
                low, high = saved[axis]
                if offsets[axis] > 0:
                    high = destination
                else:
                    low = destination
                self.set_limits(axis, low, high)
            yield
        finally:
            # shutdown の後は送れない。流している間に可動域を変えると shutdown するので、先に止める
            if self._shutdown_reason is None:
                self.stop()
                for axis, (low, high) in saved.items():
                    self.set_limits(axis, low, high)

    def endstop_pressed(self, axis: Axis) -> bool:
        """軸のリミットスイッチが押されているか."""
        message = self._transport.query(
            "endstop_query_state",
            response="endstop_state",
            oid=_ENDSTOPS[AXES.index(axis)],
        )
        return message.params["pin_value"] == self.axis(axis).endstop_pressed_level

    def disarm(self) -> None:
        """3 軸のリミットスイッチと、ロードセルの接触停止を外す."""
        for oid in _ENDSTOPS:
            self._send(
                "endstop_home",
                oid=oid,
                clock=0,
                sample_ticks=0,
                sample_count=0,
                rest_ticks=0,
                pin_value=0,
                trsync_oid=_TRSYNC,
                trigger_reason=0,
            )
        if self._load_cell is not None:
            self._disarm_contact()
        self._armed = False

    def clear_trigger(self) -> None:
        """Trsync が引かれた理由を忘れ、張っていれば外す（接触保持に入るときと、異常を知らせた後）."""
        if self._armed:
            self.disarm()
        self._trigger_reason = None

    def settle_trigger(self) -> int | None:
        """張ったものが trsync を引いていれば、その理由（引いていなければ None）.

        張ったまま、理由と ``contact`` の報告の片方だけが届いていれば、両方が揃うまで問い合わせて待つ（揃った後は
        :attr:`streaming` が偽で、:attr:`latest` が止まった位置を持つ）。張っていなければ、前に引いた理由を
        そのまま返す（原点出しのスイッチの理由が残っていることがある）.

        揃わないまま理由が届かなければ、それが残る限り、次の操作も最初の待ちで同じ :class:`ProcedureError` になる
        （:meth:`McuMotionStage.stop <pcbasm.hal.mcu.stage.McuMotionStage.stop>` も、``velocity_stream_stop`` を送る
        前に失敗する）。抜ける道は、この待ちを通らない脱力（:meth:`clear_trigger` で理由を忘れる）。transport が
        送り直すので起こりにくい.

        Raises:
            ProcedureError: 待っても揃わない
        """
        deadline = time.monotonic() + _TRIGGER_WAIT
        while True:
            reason = self._trigger_reason
            latest = self._latest
            running = latest is not None and latest.state.running
            contact = latest is not None and latest.state is StreamState.CONTACT
            if reason is None and not (self._armed and contact):
                return None
            # 張っていなければ、残っているのは前に引いた理由（原点出しのスイッチなど）で、待つものは無い
            if reason is not None and (not self._armed or not running):
                return reason
            if time.monotonic() > deadline:
                raise ProcedureError(
                    "止まったが、trsync の理由と velocity_stream の contact が揃わない"
                    f"（理由 {reason}）"
                )
            time.sleep(_POLL)
            self.query()

    @property
    def trigger_reason(self) -> int | None:
        """張ったあと trsync が引かれた理由（``trsync_state`` がまだ来ていなければ None）."""
        return self._trigger_reason

    @staticmethod
    def is_contact(reason: int | None) -> bool:
        """Trsync を引いた理由がロードセルの接触か."""
        return reason == _CONTACT_REASON

    @staticmethod
    def sensor_error(reason: int | None) -> str | None:
        """Trsync を引いた理由がロードセルの異常なら、その名前（``MONITOR`` など）.

        理由が無いか、接触かリミットスイッチなら None。どれでもない理由（0 や 9 以上）も、止まったまま黙って
        通さないよう異常とする（fail closed）.
        """
        if (
            reason is None
            or reason == _CONTACT_REASON
            or reason in _SWITCH_REASON.values()
        ):
            return None
        index = reason - _SENSOR_ERROR_REASON
        if 0 <= index < len(_SENSOR_ERRORS):
            return _SENSOR_ERRORS[index]
        return f"知らない理由 {reason}"

    @staticmethod
    def switch_axis(reason: int | None) -> Axis | None:
        """Trsync を引いた理由が、どの軸のリミットスイッチか（スイッチでなければ None）."""
        for axis, value in _SWITCH_REASON.items():
            if value == reason:
                return axis
        return None

    def emergency_stop(self) -> None:
        """即座に止める（MCU は shutdown する）。ほかの操作を待たずに送る."""
        self._transport.send("emergency_stop")

    def close(self) -> None:
        """ロードセルを止め（送れなければ諦める）、時刻の同期を止めて接続を閉じる."""
        if self._load_cell is not None:
            with contextlib.suppress(McuError):
                self._load_cell.stop()
        self._clock.close()
        self._transport.close()

    def _connect(self) -> None:
        """時刻の同期を始め、設定するか使い回して、原点が「未」の止まった状態にする."""
        config = _read_config(self._transport)
        crc = _config_crc(self._transport, self._settings)
        self._clock.start()
        if config.is_config:
            self._clean_up()
        else:
            self._configure(crc)
            self._reconfigured = True
        self._program_drivers()
        self.forget_origin(*AXES)
        self.query()
        if self._load_cell is not None:
            self._load_cell.start()

    def _configure(self, crc: int) -> None:
        for name, params in _config_commands(self._transport, self._settings):
            self._send(name, **params)
        self._send("finalize_config", crc=crc)
        if not _read_config(self._transport).is_config:
            raise McuError("MCU を設定できない")

    def _clean_up(self) -> None:
        """前のプロセスが残したもの（流したまま、張ったままの endstop と接触停止）を片付ける."""
        self.stop()
        self.disarm()

    def _program_drivers(self) -> None:
        """3 軸の TMC2209 にレジスタを書く。書くたびに IFCNT が 1 つだけ進んだことを確かめる."""
        for settings in self._settings.axes:
            address = settings.tmc_address
            count = self._read_register(address, IFCNT)
            for register, value in register_values(
                settings.tmc, 1 / settings.steps_per_mm
            ).items():
                self._transport.query(
                    "tmcuart_send",
                    response="tmcuart_response",
                    oid=_TMCUART,
                    write=encode_uart(write_datagram(address, register, value)),
                    read=0,
                )
                expected = (count + 1) % 256
                count = self._read_register(address, IFCNT)
                if count != expected:
                    raise ProcedureError(
                        f"TMC2209（アドレス {address}）のレジスタ {register:#04x} への書き込みが"
                        f"数えられない（IFCNT が {expected} ではなく {count}）"
                    )

    def _read_register(self, address: int, register: int) -> int:
        """TMC2209 のレジスタを読む（乱れた応答は読み直す）."""
        attempts = 0
        while attempts < _TMC_READ_ATTEMPTS:
            attempts += 1
            data = self._transport.query(
                "tmcuart_send",
                response="tmcuart_response",
                oid=_TMCUART,
                write=encode_uart(read_request(address, register)),
                read=_TMC_REPLY_BYTES,
            ).params["read"]
            assert isinstance(data, bytes)
            match decode_read_reply(data):
                case (answered, value) if answered == register:
                    return value
                case _:
                    continue
        raise ProcedureError(
            f"TMC2209（アドレス {address}）から {attempts} 回読んでも正しい応答が無い。"
            "ドライバの電源（24 V）と UART の配線を確かめてください"
        )

    def _arm(self, switch: Axis | None, *, contact: bool) -> None:
        """同じ trsync に 3 軸の stepper と ``velocity_stream`` を登録し、スイッチと接触停止を張る."""
        load_cell = self._load_cell
        load_settings = self._settings.load_cell
        if contact and (load_cell is None or not load_cell.ready):
            raise LoadCellFault(
                "ロードセルが読めないので動かさない（風袋がまだか、値が来ていない）"
            )
        self._trigger_reason = None
        self._send(
            "trsync_start", oid=_TRSYNC, report_clock=0, report_ticks=0, expire_reason=0
        )
        for oid in _STEPPERS:
            self._send("stepper_stop_on_trigger", oid=oid, trsync_oid=_TRSYNC)
        self._send("velocity_stream_stop_on_trigger", oid=_STREAM, trsync_oid=_TRSYNC)
        clock = to_clock32(self._clock.now() + self._clock.ticks(_SCHEDULE_AHEAD))
        if switch is not None:
            self._send(
                "endstop_home",
                oid=_ENDSTOPS[AXES.index(switch)],
                clock=clock,
                sample_ticks=self._clock.ticks(_ENDSTOP_SAMPLE),
                sample_count=_ENDSTOP_SAMPLE_COUNT,
                rest_ticks=self._clock.ticks(_ENDSTOP_REST),
                pin_value=self.axis(switch).endstop_pressed_level,
                trsync_oid=_TRSYNC,
                trigger_reason=_SWITCH_REASON[switch],
            )
        if contact:
            assert load_settings is not None
            self._send(
                "trigger_analog_home",
                oid=_TRIGGER_ANALOG,
                trsync_oid=_TRSYNC,
                trigger_reason=_CONTACT_REASON,
                error_reason=_SENSOR_ERROR_REASON,
                clock=clock,
                monitor_ticks=self._clock.ticks(1 / load_settings.sample_rate),
                monitor_max=_MONITOR_MAX,
            )
            self._contact_armed = True
        self._armed = True

    def _disarm_contact(self) -> None:
        self._send(
            "trigger_analog_home",
            oid=_TRIGGER_ANALOG,
            trsync_oid=_TRSYNC,
            trigger_reason=0,
            error_reason=0,
            clock=0,
            monitor_ticks=0,
            monitor_max=0,
        )
        self._contact_armed = False

    def _raise_load_cell_stop(self, reason: int | None) -> None:
        """Trsync の理由がロードセルの接触か異常なら、それを例外にする（異常なら理由を忘れて外す）."""
        if self.is_contact(reason):
            raise ContactStop("ロードセルの接触で止まった")
        if (error := self.sensor_error(reason)) is not None:
            self.clear_trigger()
            raise LoadCellFault(f"ロードセルの異常で止めた（{error}）")

    def _send(self, name: str, **params: ParamValue | str) -> None:
        self._transport.send(name, **params)

    def _report(self, message: Message) -> StreamReport:
        params = message.params
        name = self._transport.dictionary.enum_name(
            "velocity_stream_state", _int(params, "state")
        )
        return StreamReport(
            clock=self._clock.extend(_int(params, "clock")),
            seq=_int(params, "seq"),
            state=StreamState(name),
            position=(_int(params, "x"), _int(params, "y"), _int(params, "z")),
            velocity=(_int(params, "vx"), _int(params, "vy"), _int(params, "vz")),
        )

    # 以下、受信スレッド

    def _send_tare(self, counts: int) -> None:
        """ロードセルの風袋を ``sos_filter`` の offset にする（LoadCell
        のロックの中で、どのスレッドからも）."""
        load_cell = self._settings.load_cell
        assert load_cell is not None
        self._transport.send(
            "sos_filter_set_offset_scale",
            oid=_SOS_FILTER,
            offset=-counts,
            scale=-1 if load_cell.inverted else 1,
            scale_frac_bits=0,
            auto_offset=0,
        )

    def _on_state(self, message: Message, _received: float) -> None:
        try:
            report = self._report(message)
        except (McuError, ValueError) as error:
            _logger.debug("読めない velocity_stream_state を捨てた: %s", error)
            return
        self._latest = report
        if report.state is StreamState.CONTACT:
            self._streaming = False

    def _on_trsync(self, message: Message, _received: float) -> None:
        reason = message.params["trigger_reason"]
        if not message.params["can_trigger"] and isinstance(reason, int):
            self._trigger_reason = reason
            if self.is_contact(reason) and self._load_cell is not None:
                # 押し当てた荷重を、ホストが保持に入るまでの間に風袋へ取り込まない
                self._load_cell.pause_auto_tare(True)

    def _on_shutdown(self, message: Message, _received: float) -> None:
        if self._shutdown_reason is not None:
            return
        value = message.params.get("static_string_id")
        if not isinstance(value, int):
            self._shutdown_reason = message.name
            return
        try:
            self._shutdown_reason = self._transport.dictionary.enum_name(
                "static_string_id", value
            )
        except McuError:
            self._shutdown_reason = f"static_string_id {value}"


def open_firmware(
    settings: McuMotionSettings, *, reset: bool = False
) -> PnpMotionFirmware:
    """MCU を開いて設定し、原点が「未」の止まった状態で返す.

    MCU が同じ設定（crc）で設定済みで shutdown していなければ、reset せずに使い回す。設定が違うとき、
    shutdown しているとき、``reset`` のときは、reset してから設定する。TMC2209 は毎回書く。

    Raises:
        McuError: 開けない、pnp-motion ファームではない、設定できない、TMC2209 が応えない
    """
    transport = _open(settings.device)
    try:
        config = _read_config(transport)
        crc = _config_crc(transport, settings)
        if reset or config.is_shutdown or (config.is_config and config.crc != crc):
            _logger.info("MCU を reset する（%s）", settings.device)
            transport.send("reset")
            transport.close()
            transport = _reopen(settings.device)
            config = _read_config(transport)
            if config.is_config or config.is_shutdown:
                raise McuError("MCU を reset できない")
        return PnpMotionFirmware(transport, settings)
    except BaseException:
        transport.close()
        raise


def send_emergency_stop(device: str, *, timeout: float = 1.0) -> None:
    """開いていない MCU に緊急停止を送る（identify と emergency_stop だけを送り、設定も reset もしない）.

    MCU が shutdown を知らせるのを待ってから閉じる。

    Raises:
        McuError: 開けない、または ``timeout`` 秒で shutdown が知らされない
    """
    transport = McuTransport(device)
    try:
        transport.identify()
        done = threading.Event()
        transport.subscribe("shutdown", lambda _message, _at: done.set())
        transport.subscribe("is_shutdown", lambda _message, _at: done.set())
        transport.send("emergency_stop")
        if not done.wait(timeout):
            raise McuError(f"緊急停止のあと MCU が shutdown を {timeout} 秒知らせない")
    finally:
        transport.close()


def _open(device: str) -> McuTransport:
    """開いて identify し、pnp-motion ファームかを確かめる."""
    transport = McuTransport(device)
    try:
        dictionary = transport.identify()
        missing = [
            name for name in _REQUIRED_COMMANDS if not dictionary.has_command(name)
        ]
        if missing:
            raise McuError(f"{_NOT_PNP_MOTION}（{', '.join(missing)} が無い）")
        protocol = dictionary.constant("VELOCITY_STREAM_PROTOCOL")
        if protocol != PROTOCOL_VERSION:
            raise McuError(
                f"{_NOT_PNP_MOTION}（velocity_stream の版が {protocol}。{PROTOCOL_VERSION} が要る）"
            )
    except BaseException:
        transport.close()
        raise
    return transport


def _reopen(device: str) -> McuTransport:
    """Reset した MCU が USB に付き直すのを待って開き直す."""
    time.sleep(_RESET_SETTLE)
    deadline = time.monotonic() + _RECONNECT_TIMEOUT
    while True:
        try:
            return _open(device)
        except McuError:
            if time.monotonic() > deadline:
                raise
            time.sleep(_RECONNECT_RETRY)


def _int(params: Mapping[str, ParamValue], name: str) -> int:
    value = params[name]
    if not isinstance(value, int):
        raise McuError(f"{name} が整数ではない: {value!r}")
    return value


def _read_config(transport: McuTransport) -> _ConfigState:
    params = transport.query("get_config", response="config").params
    return _ConfigState(
        is_config=bool(params["is_config"]),
        crc=_int(params, "crc"),
        is_shutdown=bool(params["is_shutdown"]),
    )


def _config_crc(transport: McuTransport, settings: McuMotionSettings) -> int:
    dictionary = transport.dictionary
    encoded = b"".join(
        dictionary.encode(name, **params)
        for name, params in _config_commands(transport, settings)
    )
    return zlib.crc32(encoded)


def _config_commands(
    transport: McuTransport, settings: McuMotionSettings
) -> Iterator[tuple[str, dict[str, Any]]]:
    """設定の列（``finalize_config`` の前まで）."""
    axes = settings.axes
    yield "allocate_oids", {"count": _OID_COUNT}
    for oid, axis in zip(_STEPPERS, axes, strict=True):
        yield (
            "config_stepper",
            {
                "oid": oid,
                "step_pin": axis.step_pin,
                "dir_pin": axis.dir_pin,
                "invert_step": -1,
                "step_pulse_ticks": 0,
            },
        )
    for oid, axis in zip(_ENDSTOPS, axes, strict=True):
        yield (
            "config_endstop",
            {"oid": oid, "pin": axis.endstop_pin, "pull_up": int(axis.endstop_pull_up)},
        )
    for oid, axis in zip(_ENABLES, axes, strict=True):
        # 設定した直後も、shutdown したあとも、モーターは脱力させておく
        off = _enable_level(axis, False)
        yield (
            "config_digital_out",
            {
                "oid": oid,
                "pin": axis.enable_pin,
                "value": off,
                "default_value": off,
                "max_duration": 0,
            },
        )
    yield "config_trsync", {"oid": _TRSYNC}
    if settings.led_pin is not None:
        yield (
            "config_digital_out",
            {
                "oid": _LED,
                "pin": settings.led_pin,
                "value": 1,
                "default_value": 1,
                "max_duration": 0,
            },
        )
    if (load_cell := settings.load_cell) is not None:
        yield (
            "config_hx71x",
            {
                "oid": _HX71X,
                "gain_channel": load_cell.gain_channel,
                "dout_pin": load_cell.dout_pin,
                "sclk_pin": load_cell.sclk_pin,
            },
        )
        yield "config_sos_filter", {"oid": _SOS_FILTER, "max_sections": 0}
        yield (
            "config_trigger_analog",
            {"oid": _TRIGGER_ANALOG, "sos_filter_oid": _SOS_FILTER},
        )
        yield (
            "trigger_analog_set_raw_range",
            {"oid": _TRIGGER_ANALOG, "raw_min": _RAW_MIN, "raw_max": _RAW_MAX},
        )
        yield (
            "trigger_analog_set_trigger",
            {
                "oid": _TRIGGER_ANALOG,
                "trigger_analog_type": transport.dictionary.enum_value(
                    "trigger_analog_type", "gt"
                ),
                "trigger_value": round(
                    load_cell.contact.trigger_force * load_cell.counts_per_gram
                ),
            },
        )
        yield (
            "hx71x_attach_trigger_analog",
            {"oid": _HX71X, "trigger_analog_oid": _TRIGGER_ANALOG},
        )
    frequency = transport.dictionary.constant("CLOCK_FREQ")
    assert isinstance(frequency, int)
    yield (
        "config_tmcuart",
        {
            "oid": _TMCUART,
            "rx_pin": settings.tmc_rx_pin,
            "pull_up": int(settings.tmc_rx_pull_up),
            "tx_pin": settings.tmc_tx_pin,
            "bit_time": round(frequency / settings.tmc_baud),
        },
    )
    yield (
        "config_velocity_stream",
        {
            "oid": _STREAM,
            "stepper_x_oid": _STEPPERS[0],
            "stepper_y_oid": _STEPPERS[1],
            "stepper_z_oid": _STEPPERS[2],
            "invert_dir": sum(1 << i for i, axis in enumerate(axes) if axis.invert_dir),
            # 列挙は encode が引数の名前から解けないので、値に直して渡す
            "kinematics": transport.dictionary.enum_value(
                "velocity_stream_kinematics", settings.kinematics
            ),
        },
    )


def _enable_level(settings: AxisSettings, energized: bool) -> int:
    """モーターを ``energized`` にするときの enable のピンの値."""
    if settings.invert_enable:
        return 0 if energized else 1
    return 1 if energized else 0
