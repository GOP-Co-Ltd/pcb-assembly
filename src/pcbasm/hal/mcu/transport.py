"""MCU とのシリアル通信: ブロックの送り直し、応答の振り分け、問い合わせ.

仕様として読んだもの（コードは写していない。pcbasm は MIT、出典は GPLv3）:

- ``src/firmware/pnp-motion/klipper/docs/Protocol.md`` の「Message flow」と「Data Dictionary」
- ファームの C ソース ``klipper/src/command.c``（ブロックを受けたときの seq の検査、ack と nak の返し方）、
  ``klipper/src/sched.c``（``shutdown`` と ``is_shutdown`` を返す場面）、
  ``klipper/src/basecmd.c`` と ``klipper/src/generic/armcm_reset.c``（``HF_IN_SHUTDOWN`` の印が付いた、
  shutdown の間も受け付けるコマンド）

ホストから MCU へのブロックは届くことを保証する。MCU は順番どおりのブロックを処理してから、
次に待つ seq を載せた空のブロック（ack）を返す。順番の合わないブロックは捨て、待っている seq の空の
ブロック（nak）を返す。MCU が返すブロックは、応答でも ack でも、いつも「次に待つ seq」を載せる。
ここから次のように決めた。

- **番号:** seq は 4 bit しかないので、ホストの側では通し番号で持ち、線に出すときに下位 4 bit にする。
  ack を待つブロックは ``window`` 個までにし、ack の seq から通し番号を一意に引けるようにする
- **ack:** 受けたブロックの seq が、ack を待つ先頭より先を指していれば、その手前までが届いた
- **nak:** MCU は処理してから ack を返すので、ack の seq は空のブロックごとに 1 ずつ進む。空のブロックが
  直前の空のブロックと同じ seq を載せていれば、それは nak で、待っているブロックが失われた。
  すぐに送り直す（同じ先頭について 1 回だけ。窓の残りのブロックごとに nak が続くため）
- **期限:** ack が ``retransmit_timeout`` 来なければ、ack を待つブロックをすべて送り直す。先頭のブロックを
  最初に送ってから ``disconnect_timeout`` 過ぎても ack が来なければ、接続が切れたとみなす
- **緊急停止:** ``emergency_stop`` は、ack を待つブロックが ``window`` に達していても並ばせずにすぐ送る
  （seq を一意に引ける 15 個までは窓を超えてよい）
- **最初の同期:** 開いた時点では、MCU が待つ seq は分からない（前のホストが話した後かもしれない）。
  最初に受けたブロックの seq を MCU が待つ番号とみなし、ack を待つブロックをその番号から振り直して
  送る。読み違えても（nak の seq がたまたま「届いた」に見えても）、最初の送信は identify の問い合わせ
  なので、応答が来なければ送り直して追いつく

MCU から届くブロックは送り直されないので、問い合わせ（:meth:`McuTransport.query`）は、コマンドの
ブロックに ack が来たのに応答が来なければ、コマンドを送り直す。MCU が自分から送るメッセージ
（``stats`` など）と、待っても購読してもいない応答は、debug のログに残して捨てる。

``shutdown`` か ``is_shutdown`` を受けたら、それ以降はファームが shutdown の間も受け付けるコマンド
（``identify``、``get_clock``、``get_uptime``、``get_config``、``emergency_stop``、``clear_shutdown``、
``reset``）だけを送る。ほかの送信と問い合わせは :class:`McuShutdown` になる。
``clear_shutdown`` に ack が来たら shutdown を抜けたとみなし、ほかのコマンドもまた送る（ファームが
まだ shutdown を処理していて解除を無視したときは、次のコマンドへの ``is_shutdown`` で shutdown に
戻る）。``reset`` を送ると MCU が再起動して接続が切れるので、続けるには開き直す。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

import attrs
import serial

from pcbasm.hal.mcu.dictionary import (
    DataDictionary,
    Message,
    ParamValue,
    identify_request,
    parse_identify_response,
)
from pcbasm.hal.mcu.errors import McuError, McuShutdown
from pcbasm.hal.mcu.wire import Block, BlockReader, encode_block

_logger = logging.getLogger(__name__)

_SEQ_MODULO = 16
_IDENTIFY_CHUNK = 40  # identify_response が 1 ブロック（内容 59 バイト）に収まる大きさ
_QUERY_RETRY = 0.25  # 問い合わせの応答をこれだけ待って来なければ、コマンドを送り直す
_READ_TIMEOUT = 0.01  # 受信スレッドが期限を見に起きる間隔
_SHUTDOWN_RESPONSES = frozenset({"shutdown", "is_shutdown"})
# ファームが shutdown の間も受け付けるコマンド（HF_IN_SHUTDOWN）
_ACCEPTED_IN_SHUTDOWN = frozenset(
    {
        "identify",
        "get_clock",
        "get_uptime",
        "get_config",
        "emergency_stop",
        "clear_shutdown",
        "reset",
    }
)
# ack を待つブロックの窓を超えてすぐ送るコマンド
_URGENT = frozenset({"emergency_stop"})

type Callback = Callable[[Message, float], None]


@attrs.define(eq=False)
class _Outgoing:
    """送るブロック。``seq`` は通し番号で、線に出すときに振る（それまでは -1）.

    ``clears_shutdown`` は ``clear_shutdown`` のブロックで、ack が来たら shutdown を抜けたとみなす。
    """

    content: bytes
    clears_shutdown: bool = False
    seq: int = -1
    first_sent: float = 0.0
    last_sent: float = 0.0


@attrs.define
class _Waiter:
    """問い合わせが待つ応答."""

    response: str
    oid: int | None
    accept: Callable[[Message], bool]
    message: Message | None = None

    def matches(self, message: Message) -> bool:
        return (
            self.message is None
            and message.name == self.response
            and (self.oid is None or message.params.get("oid") == self.oid)
            and self.accept(message)
        )


class McuTransport:
    """シリアルポートの向こうの MCU と話す.

    開くと受信スレッド（daemon）が動き出す。:meth:`close` で止めてポートを閉じる。
    送信と問い合わせはどのスレッドから呼んでもよい。購読の callback は受信スレッドで呼ばれる。
    """

    def __init__(
        self,
        device: str,
        *,
        retransmit_timeout: float = 0.1,
        window: int = 8,
        disconnect_timeout: float = 1.0,
    ) -> None:
        try:
            # USB CDC なので baudrate は意味を持たない。同じポートを別のプロセスと取り合わない
            self._serial = serial.Serial(device, timeout=_READ_TIMEOUT, exclusive=True)
        except OSError as error:  # serial.SerialException は OSError の派生
            raise McuError(f"MCU のポート {device} を開けない: {error}") from error
        try:
            self._serial.reset_input_buffer()
        except OSError as error:
            self._serial.close()
            raise McuError(f"MCU のポート {device} を開けない: {error}") from error
        self._device = device
        self._retransmit_timeout = retransmit_timeout
        self._window = window
        self._disconnect_timeout = disconnect_timeout
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._reader = BlockReader()
        self._dictionary: DataDictionary | None = None
        self._next_seq = 0
        self._unacked: deque[_Outgoing] = deque()
        self._queued: deque[_Outgoing] = deque()
        self._synced = False
        self._last_empty_seq: int | None = None
        self._nak_resent = False
        self._waiters: list[_Waiter] = []
        self._subscriptions: dict[tuple[str, int | None], Callback] = {}
        self._shutdown_reason: str | None = None
        self._broken: str | None = None
        self._closed = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._receive, name=f"mcu-transport {device}", daemon=True
        )
        self._thread.start()

    def identify(self, *, timeout: float = 1.0) -> DataDictionary:
        """MCU からデータ辞書を読み出す。以後の送信と受信はこの辞書で符号化・復号する.

        ``timeout`` は 1 つの chunk の応答を待つ長さ。
        """
        blob = bytearray()
        while True:
            offset = len(blob)
            message = self._request(
                "identify",
                identify_request(offset, _IDENTIFY_CHUNK),
                response="identify_response",
                oid=None,
                timeout=timeout,
                accept=lambda message, offset=offset: message.params["offset"]
                == offset,
            )
            data = message.params["data"]
            assert isinstance(data, bytes)
            if not data:
                break
            blob += data
        dictionary = DataDictionary.from_blob(bytes(blob))
        with self._lock:
            self._dictionary = dictionary
        return dictionary

    @property
    def dictionary(self) -> DataDictionary:
        """:meth:`identify` で読んだデータ辞書。まだ読んでいなければ :class:`McuError`."""
        if self._dictionary is None:
            raise McuError("identify の前はデータ辞書が無い")
        return self._dictionary

    def send(self, name: str, **params: ParamValue | str) -> None:
        """コマンドを送る（ack は待たない）."""
        content = self.dictionary.encode(name, **params)
        with self._lock:
            self._raise_if_unusable(name)
            block = self._enqueue(content, urgent=name in _URGENT)
            block.clears_shutdown = name == "clear_shutdown"

    def query(
        self,
        name: str,
        *,
        response: str,
        oid: int | None = None,
        timeout: float = 1.0,
        **params: ParamValue | str,
    ) -> Message:
        """コマンドを送り、応答 ``response`` を待って返す.

        ``oid`` を渡すと、コマンドの引数 ``oid`` として送り、同じ ``oid`` の応答だけを待つ。``timeout`` 秒を
        過ぎたら :class:`McuError`。

        コマンドのブロックに ack が来たのに応答が来なければ、コマンドを送り直す。応答だけが失われた
        ときは MCU が同じコマンドを 2 度実行するので、問い合わせには実行し直しても結果の変わらない
        コマンド（状態を読むもの）だけを使う。状態を変えるコマンドは :meth:`send` で送る。
        """
        if oid is not None:
            params["oid"] = oid
        content = self.dictionary.encode(name, **params)
        return self._request(
            name,
            content,
            response=response,
            oid=oid,
            timeout=timeout,
            accept=lambda _: True,
        )

    def subscribe(
        self, response: str, callback: Callback, *, oid: int | None = None
    ) -> None:
        """応答 ``response`` を受けるたびに ``callback(message, 受けた時刻)`` を呼ぶ.

        時刻は ``time.monotonic()``。``oid`` を渡せばその oid の応答だけ、省けばすべての oid の応答を受ける。
        同じ応答と oid の組に登録し直すと、前の callback と置き換わる。
        """
        with self._lock:
            self._subscriptions[(response, oid)] = callback

    def close(self) -> None:
        """受信スレッドを止めてポートを閉じる。待っている問い合わせは :class:`McuError` になる."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._changed.notify_all()
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join()
        self._serial.close()

    def _request(
        self,
        name: str,
        content: bytes,
        *,
        response: str,
        oid: int | None,
        timeout: float,
        accept: Callable[[Message], bool],
    ) -> Message:
        waiter = _Waiter(response=response, oid=oid, accept=accept)
        deadline = time.monotonic() + timeout
        with self._lock:
            self._raise_if_unusable(name)
            self._waiters.append(waiter)
            try:
                block: _Outgoing | None = None
                while True:
                    # ack を待つあいだはブロックの送り直しに任せる（重ねて送ると MCU が 2 度実行する）
                    if block is None or not self._is_pending(block):
                        block = self._enqueue(content, urgent=False)
                    retry_at = min(deadline, time.monotonic() + _QUERY_RETRY)
                    while waiter.message is None and self._usable(name):
                        remaining = retry_at - time.monotonic()
                        if remaining <= 0:
                            break
                        self._changed.wait(remaining)
                    if waiter.message is not None:
                        return waiter.message
                    self._raise_if_unusable(name)
                    if time.monotonic() >= deadline:
                        raise McuError(
                            f"MCU から {response} の応答が {timeout} 秒来ない"
                        )
            finally:
                self._waiters.remove(waiter)

    # 以下、self._lock を持って呼ぶ

    def _usable(self, name: str) -> bool:
        if self._closed or self._broken:
            return False
        return self._shutdown_reason is None or name in _ACCEPTED_IN_SHUTDOWN

    def _raise_if_unusable(self, name: str) -> None:
        if self._closed:
            raise McuError(f"MCU との接続は閉じている（{self._device}）")
        if self._shutdown_reason is not None and name not in _ACCEPTED_IN_SHUTDOWN:
            raise McuShutdown(self._shutdown_reason)
        if self._broken is not None:
            raise McuError(self._broken)

    def _enqueue(self, content: bytes, *, urgent: bool) -> _Outgoing:
        block = _Outgoing(content=content)
        # 窓を超えても、ack の seq（4 bit）から通し番号を一意に引ける数までは送ってよい
        limit = _SEQ_MODULO - 1 if urgent else self._window
        if len(self._unacked) >= limit:
            self._queued.append(block)
        else:
            self._transmit(block)
        return block

    def _transmit(self, block: _Outgoing) -> None:
        now = time.monotonic()
        block.seq = self._next_seq
        block.first_sent = block.last_sent = now
        self._next_seq += 1
        self._unacked.append(block)
        self._write(encode_block(block.seq, block.content))

    def _is_pending(self, block: _Outgoing) -> bool:
        """``block`` がまだ ack を待っているか、送る順番を待っているか."""
        return any(other is block for other in (*self._unacked, *self._queued))

    def _resend_all(self) -> None:
        now = time.monotonic()
        for block in self._unacked:
            block.last_sent = now
            self._write(encode_block(block.seq, block.content))

    def _write(self, frame: bytes) -> None:
        if self._broken is not None or self._closed:
            return
        try:
            self._serial.write(frame)
        except OSError as error:  # serial.SerialException は OSError の派生
            self._fail(f"MCU へ書けない（{self._device}）: {error}")

    def _fail(self, reason: str) -> None:
        if self._broken is None:
            _logger.warning("%s", reason)
            self._broken = reason
        self._changed.notify_all()

    def _first_unacked(self) -> int:
        return self._unacked[0].seq if self._unacked else self._next_seq

    def _to_absolute(self, wire_seq: int) -> int | None:
        """線の seq を、ack を待つ先頭から次に振る番号までの通し番号に直す（範囲外なら None）."""
        first = self._first_unacked()
        candidate = first + (wire_seq - first) % _SEQ_MODULO
        return candidate if candidate <= self._next_seq else None

    def _on_sequence(self, block: Block) -> None:
        first = self._first_unacked()
        acked = self._to_absolute(block.seq)
        if not self._synced:
            self._synced = True
            if acked is None or acked == first:
                # MCU が待つ番号に振り直して送る
                shift = (block.seq - first) % _SEQ_MODULO
                for outgoing in self._unacked:
                    outgoing.seq += shift
                self._next_seq += shift
                self._resend_all()
                self._remember_empty(block)
                return
        if acked is not None and acked > first:
            while self._unacked and self._unacked[0].seq < acked:
                if self._unacked.popleft().clears_shutdown:
                    self._shutdown_reason = None
            self._nak_resent = False
            while self._queued and len(self._unacked) < self._window:
                self._transmit(self._queued.popleft())
            self._changed.notify_all()
        elif (
            not block.content
            and block.seq == self._last_empty_seq
            and self._unacked
            and not self._nak_resent
        ):
            self._nak_resent = True
            self._resend_all()
        self._remember_empty(block)

    def _remember_empty(self, block: Block) -> None:
        if not block.content:
            self._last_empty_seq = block.seq

    def _check_timers(self) -> None:
        if not self._unacked or self._broken is not None:
            return
        now = time.monotonic()
        oldest = self._unacked[0]
        if now - oldest.first_sent > self._disconnect_timeout:
            self._fail(
                f"MCU から ack が {self._disconnect_timeout} 秒来ない（{self._device}）"
            )
        elif now - oldest.last_sent > self._retransmit_timeout:
            self._resend_all()

    # 以下、受信スレッド

    def _receive(self) -> None:
        try:
            while not self._stop.is_set():
                self._receive_once()
            return
        except OSError as error:  # serial.SerialException は OSError の派生
            reason = f"MCU から読めない（{self._device}）: {error}"
        except Exception as error:
            # 受信スレッドが黙って止まると、ack の期限も見なくなり接続断を検出できない
            _logger.exception("MCU の受信スレッドが止まった")
            reason = f"MCU の受信スレッドが止まった（{self._device}）: {error!r}"
        with self._lock:
            if not self._closed:
                self._fail(reason)

    def _receive_once(self) -> None:
        data = self._serial.read(self._serial.in_waiting or 1)
        received = time.monotonic()
        for block in self._reader.feed(data):
            with self._lock:
                self._on_sequence(block)
            if block.content:
                self._dispatch(block.content, received)
        with self._lock:
            self._check_timers()

    def _dispatch(self, content: bytes, received: float) -> None:
        try:
            messages = self._decode(content)
        except McuError as error:
            _logger.debug("MCU から読めないブロックを捨てた: %s", error)
            return
        for message in messages:
            self._deliver(message, received)

    def _decode(self, content: bytes) -> list[Message]:
        dictionary = self._dictionary
        if dictionary is not None:
            return dictionary.decode(content)
        offset, data = parse_identify_response(content)
        return [
            Message(name="identify_response", params={"offset": offset, "data": data})
        ]

    def _deliver(self, message: Message, received: float) -> None:
        oid = message.params.get("oid")
        with self._lock:
            if message.name in _SHUTDOWN_RESPONSES and self._shutdown_reason is None:
                self._shutdown_reason = self._shutdown_text(message)
                _logger.warning("MCU が shutdown した: %s", self._shutdown_reason)
            waiters = [waiter for waiter in self._waiters if waiter.matches(message)]
            for waiter in waiters:
                waiter.message = message
            callbacks = [
                callback
                for key in {(message.name, oid), (message.name, None)}
                if (callback := self._subscriptions.get(key)) is not None
            ]
            self._changed.notify_all()
        for callback in callbacks:
            try:
                callback(message, received)
            except Exception:
                _logger.exception("MCU の応答 %s の callback が失敗した", message.name)
        if not waiters and not callbacks:
            _logger.debug("待っていない MCU の応答を捨てた: %s", message)

    def _shutdown_text(self, message: Message) -> str:
        value = message.params.get("static_string_id")
        if not isinstance(value, int):
            return message.name
        try:
            return self.dictionary.enum_name("static_string_id", value)
        except McuError:
            return f"static_string_id {value}"
