"""ロードセル（HX711）の値の読み取り、グラムへの換算、タール.

pnp-motion ファームの ``hx71x`` が送る ``sensor_bulk_data`` を受け、ステージに掛かる荷重 [g] にする。
:class:`LoadTracker` は時刻を引数で受ける純粋なロジック、:class:`LoadCell` は MCU との I/O の包み。

仕様として読んだもの（コードは写していない。pcbasm は MIT、出典は GPLv3）:

- ファームの C ソース ``klipper/src/sensor_hx71x.c``
    - ``config_hx71x oid gain_channel dout_pin sclk_pin``。``gain_channel`` は 1〜4 で、外れると shutdown する
    - ``query_hx71x oid rest_ticks``。0 で止めて chip を power down する。0 以外は ``last_error`` と
      ``sensor_bulk`` の sequence を 0 に戻して読み直す
    - タイマは、データ待ちの間は ``rest_ticks`` ごとにデータ出力のピンを見る。準備できたら ``8 × rest_ticks`` 後に
      次を見る。そのときまでに task が読み終えていなければ、読み出しが遅れた（READ_TOO_LONG）とする
    - サンプルは 4 byte の little endian。24 bit を符号拡張した値か、誤りの値（``1 << 31`` = DESYNC、
      ``1 << 30`` = READ_TOO_LONG）。誤りは ``query_hx71x`` を送り直すまで出続ける
- ``klipper/src/sensor_bulk.c`` と ``sensor_bulk.h``: ``data[51]`` なので 1 通に 12 サンプル入る。1 通ごとに
  sequence（uint16）が 1 進む。hx71x はため切ってから送る（80 SPS では約 150 ms ごとにまとめて届く）
- ``klipper/docs/Config_Reference.md`` の ``[load_cell]`` の hx711 の項（gain、sample_rate、sensor_orientation）
- HX711 データシート（gain は 25 / 26 / 27 パルスで A-128 / B-32 / A-64、出力レートは RATE ピンで 10 か 80 SPS）

決めたこと:

- **rest_ticks:** ``CLOCK_FREQ // (sample_rate × 16)``（80 SPS で約 0.78 ms）。``8 × rest_ticks`` が半周期に
  なり、task が読むまでの遅れより長く、1 サンプルの周期より短い。データの準備を見つける遅れは
  ``rest_ticks`` 以下に収まる
- **時刻:** 1 通の最後のサンプルを受けた時刻とする。その前の i 番目は ``受けた時刻 - (n-1-i)/sample_rate``
- **sequence の欠落:** 期待した値と違えば debug ログに残し、安定の窓を捨てる。期待値は「受けた値 + 1」に
  合わせ直す（uint16 で一周する）。``query_hx71x`` を送ったら期待値を 0 に戻す
- **誤りのサンプル:** 荷重を None にして誤りを出す。``query_hx71x`` を送り直して回復させる（1 秒に 1 回まで）。
  正しいサンプルが来たら誤りを消す
- **値が来ない:** 最後のサンプル（か読み始め）から「古いとみなす時間」来なければ「値が来ない」とする。
  古いとみなす時間は 0.5 s と、1 通の間隔（12 サンプル）の 2 倍の長いほう（80 SPS で 0.5 s、10 SPS で 2.4 s）
- **グラム:** ``sign × (値 − タール値) / counts_per_gram``。sign は normal が +1、inverted が −1
- **最初のタール:** 読み始めて最初の 16 サンプルの平均（安定かどうかは問わない）。それまでは None
- **自動のタール:** Z が止まっている（Z を動かす指令から 1 s たった）間のサンプルが 1 s ぶん続き、その幅
  （最大 − 最小）が 0.5 g 以下で、平均と今のタール値の差が 2 g 以下なら、平均を新しいタールにして窓を空にする。
  止まっている間はおよそ 1 秒ごとに取り直す（Z の指令からは約 2 s）。差の上限は「接触していない」の近似で、
  押し続けた荷重や載せたものをタールに取り込まないためのもの。温度などのゆっくりしたずれだけを追う。
  上限を超えてずれたら（たとえば起動したときに何かが当たっていた）、手でタールする
- **手でタール:** 最後のサンプルから 0.25 s の間の正しいサンプルの平均。最後のサンプルが古いか、
  誤りが出ていれば受けない。安定かどうかは問わない
- **MCU のタール値:** タール値が変わるたびに（最初、自動、手で）``on_tare`` に丸めた counts を渡す。ファームはそれを
  ``sos_filter`` の offset にして、接触停止の判定に使う（:mod:`pcbasm.hal.mcu.firmware` の「接触停止」）
- **接触保持の間:** 自動のタールをしない（:meth:`LoadCell.pause_auto_tare`。押し当てた荷重を取り込まない）。
  離したかは、保持に入ってから後の連続 100 ms（80 SPS で 8 個）のサンプルが、すべて ``release_force`` を
  下回ったかで見る（:meth:`LoadTracker.released`）
- **区間の荷重の幅:** Z が止まっている区間と動いている区間（Z の指令から 1 s まで）の、荷重の最小と最大を
  区間が終わるたびに info ログに出す。操作パネルは最後の 1 サンプルを間をおいて出すだけなので、加速で出る
  一瞬の見かけの荷重は、このログで読む（受け入れ試験 A34）。誤りのサンプルや読み直しでは、その区間を捨てる
- **スレッド:** ``sensor_bulk_data`` の callback は受信スレッドで、transport のロックの外で呼ばれる。そこから
  使うのは ``transport.send`` だけ（``query`` は受信スレッドを待つのでデッドロックする）。:class:`LoadTracker`
  は :class:`LoadCell` のロックで守る
"""

from __future__ import annotations

import enum
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence

import attrs

from pcbasm.hal.mcu.dictionary import Message
from pcbasm.hal.mcu.settings import LoadCellSettings
from pcbasm.hal.mcu.transport import McuTransport

_logger = logging.getLogger(__name__)

_BYTES_PER_SAMPLE = 4
_SEQUENCE_MASK = 0xFFFF
_REST_DIVISOR = 16  # rest_ticks = 1 サンプルの周期の 16 分の 1
_INITIAL_TARE_SAMPLES = 16
_QUIET_SPAN = 1.0  # 自動のタールに要る、止まっている長さ [s]
_QUIET_AFTER_MOTION = 1.0  # Z を動かす指令のあと、止まったとみなすまで [s]
_MANUAL_TARE_SPAN = 0.25  # [s]
_RELEASE_SPAN = 0.1  # 離したとみなすのに要る、release_force を下回り続ける長さ [s]
_STALE_AFTER = 0.5  # 古いとみなす時間の下限 [s]
_SAMPLES_PER_MESSAGE = 12  # sensor_bulk.h の data[51] に入るサンプルの数
_REQUERY_INTERVAL = 1.0  # [s]
_NOT_ARRIVING = "ロードセルの値が来ない"
# 自動のタールで、安定とみなす 1 s の間の荷重の幅 [g]（仮の値。受け入れ試験 A34 で測って直す）
_TARE_SPREAD = 0.5
# 自動のタールで追う、今のタール値からの差の上限 [g]（仮の値。受け入れ試験 A34 で測って直す）
_TARE_DRIFT = 2.0


class SampleError(enum.Enum):
    """``hx71x`` が値の代わりに送る誤り（値はサンプルの 32 bit）."""

    DESYNC = 1 << 31
    READ_TOO_LONG = 1 << 30

    @property
    def message(self) -> str:
        """表示する文."""
        if self is SampleError.DESYNC:
            return "HX711 の読み出しが乱れた（DESYNC）"
        return "HX711 の読み出しが間に合わなかった（READ_TOO_LONG）"


def decode_samples(data: bytes) -> list[int | SampleError]:
    """``sensor_bulk_data`` の ``data`` をサンプル（符号付きの値か誤り）の列にする.

    Raises:
        ValueError: 長さが 4 byte の倍数ではない
    """
    if len(data) % _BYTES_PER_SAMPLE:
        raise ValueError(f"sensor_bulk_data の長さが 4 の倍数ではない: {len(data)}")
    samples: list[int | SampleError] = []
    for offset in range(0, len(data), _BYTES_PER_SAMPLE):
        chunk = data[offset : offset + _BYTES_PER_SAMPLE]
        raw = int.from_bytes(chunk, "little")
        try:
            samples.append(SampleError(raw))
        except ValueError:
            samples.append(int.from_bytes(chunk, "little", signed=True))
    return samples


@attrs.frozen
class LoadSection:
    """Z が止まっている、または動いている区間の荷重の幅.

    Attributes:
        quiet: Z が止まっている区間か
        seconds: 区間の最初と最後のサンプルの間 [s]
        low: 荷重の最小 [g]
        high: 荷重の最大 [g]
    """

    quiet: bool
    seconds: float
    low: float
    high: float


@attrs.frozen
class LoadReading:
    """荷重の読み.

    Attributes:
        grams: 荷重 [g]（押される向きが正）。まだ読めていないか、誤りが出ていれば None
        error: 読めない理由（読めているか、読み始めたばかりなら None）
    """

    grams: float | None
    error: str | None = None


class LoadTracker:
    """HX711 のサンプルから荷重を出し、タールする（時刻は引数で受ける。スレッドを守らない）.

    進め方はモジュールの docstring の「決めたこと」。
    """

    def __init__(self, settings: LoadCellSettings) -> None:
        self._settings = settings
        self._sign = -1.0 if settings.inverted else 1.0
        self._quiet_count = math.ceil(settings.sample_rate * _QUIET_SPAN)
        self._release_count = math.ceil(settings.sample_rate * _RELEASE_SPAN)
        self._stale_after = max(
            _STALE_AFTER, 2 * _SAMPLES_PER_MESSAGE / settings.sample_rate
        )
        self._tare: float | None = None
        self._initial: list[int] = []
        self._quiet: deque[int] = deque(maxlen=self._quiet_count)
        self._recent: deque[tuple[float, int]] = deque()
        self._last_at: float | None = None
        self._started_at: float | None = None
        self._error: SampleError | None = None
        self._section: _Section | None = None
        self._finished: LoadSection | None = None

    def note_start(self, at: float) -> None:
        """読み始めた（``query_hx71x`` を送った）.

        値が来ないことを、この時刻からも数える.
        """
        self._started_at = at
        self._quiet.clear()
        self._section = None

    def feed(
        self,
        samples: Sequence[int],
        at: float,
        *,
        quiet: bool,
        auto_tare: bool = True,
    ) -> int | None:
        """正しいサンプルを足す.

        Args:
            samples: 古い順のサンプル
            at: 最後のサンプルの時刻 [s]
            quiet: Z が止まっている間のサンプルか
            auto_tare: 自動のタールをしてよいか（接触保持の間は False）

        Returns:
            タールが変わったら、新しいタール値（丸めた counts）。変わらなければ None
        """
        if not samples:
            return None
        self._error = None
        period = 1.0 / self._settings.sample_rate
        count = len(samples)
        for index, sample in enumerate(samples):
            self._recent.append((at - (count - 1 - index) * period, sample))
        self._last_at = at
        while self._recent and self._recent[0][0] < at - self._stale_after:
            self._recent.popleft()
        if self._tare is None:
            self._initial.extend(samples)
            if len(self._initial) < _INITIAL_TARE_SAMPLES:
                return None
            self._tare = _mean(self._initial[:_INITIAL_TARE_SAMPLES])
            self._initial.clear()
            return round(self._tare)
        self._note_section(
            [self._grams(sample) for sample in samples],
            at - (count - 1) * period,
            at,
            quiet=quiet,
        )
        if not (quiet and auto_tare):
            self._quiet.clear()
            return None
        changed: float | None = None
        for sample in samples:
            self._quiet.append(sample)
            if len(self._quiet) < self._quiet_count:
                continue
            spread = (
                max(self._quiet) - min(self._quiet)
            ) / self._settings.counts_per_gram
            mean = _mean(self._quiet)
            drift = abs(mean - self._tare) / self._settings.counts_per_gram
            if spread <= _TARE_SPREAD and drift <= _TARE_DRIFT:
                changed = mean
                self._quiet.clear()
        if changed is None:
            return None
        self._tare = changed
        return round(changed)

    def take_section(self) -> LoadSection | None:
        """終わった区間の荷重の幅を受け取る（前に受け取ってから終わった区間が無ければ None）.

        区間は Z が止まっているかどうかが変わったところで終わる。続けて終わったときは最後の区間だけを残す.
        """
        finished, self._finished = self._finished, None
        return finished

    def note_gap(self) -> None:
        """サンプルが欠けた（sequence が飛んだ）。安定の窓を捨てる."""
        self._quiet.clear()

    def note_error(self, error: SampleError, at: float) -> None:
        """誤りのサンプルを受けた。正しいサンプルが来るまで荷重を出さない."""
        self._error = error
        self._last_at = at
        self._quiet.clear()
        self._recent.clear()
        self._section = None

    def tare(self, now: float) -> int:
        """最後のサンプルから 0.25 s の間の平均をタールにする.

        Returns:
            新しいタール値（丸めた counts）

        Raises:
            ValueError: 誤りが出ている、または最後の正しいサンプルが古い
        """
        if self._error is not None:
            raise ValueError(f"ロードセルが読めない: {self._error.message}")
        if not self._recent or now - self._recent[-1][0] > self._stale_after:
            raise ValueError("ロードセルの値が来ていない")
        latest = self._recent[-1][0]
        values = [
            sample for at, sample in self._recent if at >= latest - _MANUAL_TARE_SPAN
        ]
        self._tare = _mean(values)
        self._initial.clear()
        self._quiet.clear()
        return round(self._tare)

    def ready(self, now: float) -> bool:
        """タールしてあり、誤りが無く、最後の正しいサンプルが古くない."""
        return (
            self._tare is not None
            and self._error is None
            and bool(self._recent)
            and now - self._recent[-1][0] <= self._stale_after
        )

    def released(self, since: float, now: float) -> bool:
        """``since`` より後のサンプルの最後の 100 ms ぶんが、すべて ``release_force`` を下回ったか.

        誤りが出ているか、最後の正しいサンプルが古ければ偽（離したとみなさない）。
        """
        if not self.ready(now):
            return False
        after = [sample for at, sample in self._recent if at > since]
        if len(after) < self._release_count:
            return False
        release = self._settings.contact.release_force
        return all(
            self._grams(sample) < release for sample in after[-self._release_count :]
        )

    def reading(self, now: float) -> LoadReading:
        """今の荷重（最後の正しいサンプル）."""
        if self._error is not None:
            return LoadReading(None, self._error.message)
        reference = max(
            (at for at in (self._last_at, self._started_at) if at is not None),
            default=None,
        )
        if reference is not None and now - reference > self._stale_after:
            return LoadReading(None, _NOT_ARRIVING)
        if self._tare is None or not self._recent:
            return LoadReading(None)
        return LoadReading(self._grams(self._recent[-1][1]))

    def _grams(self, sample: int) -> float:
        """今のタール値からの荷重 [g]（タールしてから呼ぶ）."""
        assert self._tare is not None
        return self._sign * (sample - self._tare) / self._settings.counts_per_gram

    def _note_section(
        self, grams: Sequence[float], first: float, last: float, *, quiet: bool
    ) -> None:
        """区間の幅に足す。止まっているかどうかが変われば、それまでの区間を終える."""
        section = self._section
        if section is not None and section.quiet != quiet:
            self._finished = section.finish()
            section = None
        if section is None:
            section = self._section = _Section(quiet, first)
        section.add(grams, last)


class _Section:
    """終わっていない区間."""

    def __init__(self, quiet: bool, first: float) -> None:
        self.quiet = quiet
        self._first = first
        self._last = first
        self._low = math.inf
        self._high = -math.inf

    def add(self, grams: Sequence[float], last: float) -> None:
        self._last = last
        self._low = min(self._low, *grams)
        self._high = max(self._high, *grams)

    def finish(self) -> LoadSection:
        return LoadSection(self.quiet, self._last - self._first, self._low, self._high)


class LoadCell:
    """MCU の ``hx71x`` を動かし、受けたサンプルを :class:`LoadTracker` に渡す.

    :meth:`reading`・:meth:`tare`・:meth:`note_z_motion`・:attr:`ready`・:meth:`released`・
    :meth:`pause_auto_tare` はどのスレッドからでも呼べる。

    Args:
        transport: MCU との接続
        settings: ロードセルの設定
        oid: ``hx71x`` の oid
        clock_freq: MCU の ``CLOCK_FREQ``
        on_tare: タール値が変わるたびに、新しいタール値（丸めた counts）で呼ぶ。:class:`LoadCell` のロックの中で、
            受信スレッドか :meth:`tare` を呼んだスレッドから呼ぶので、``transport.send`` だけを使う
    """

    def __init__(
        self,
        transport: McuTransport,
        settings: LoadCellSettings,
        *,
        oid: int,
        clock_freq: int,
        on_tare: Callable[[int], None],
    ) -> None:
        self._transport = transport
        self._settings = settings
        self._oid = oid
        self._on_tare = on_tare
        self._paused = False
        self._rest_ticks = clock_freq // (settings.sample_rate * _REST_DIVISOR)
        self._lock = threading.Lock()
        self._tracker = LoadTracker(settings)
        self._expected_sequence = 0
        self._z_moved_at = -math.inf
        self._requeried_at = -math.inf
        self._started = False
        transport.subscribe("sensor_bulk_data", self._on_data, oid=oid)

    def start(self) -> None:
        """読み始める（``query_hx71x``）."""
        with self._lock:
            self._query()
            self._started = True

    def stop(self) -> None:
        """読むのを止め、HX711 を power down する（:meth:`start` の前なら何も送らない）."""
        with self._lock:
            if not self._started:
                return
            self._started = False
        self._transport.send("query_hx71x", oid=self._oid, rest_ticks=0)

    def reading(self) -> LoadReading:
        """今の荷重."""
        with self._lock:
            return self._tracker.reading(time.monotonic())

    @property
    def ready(self) -> bool:
        """接触停止に使える: タールしてあり（MCU にも送ってあり）、誤りが無く、値が来ている."""
        with self._lock:
            return self._tracker.ready(time.monotonic())

    def released(self, since: float) -> bool:
        """荷重が ``release_force`` を 100 ms 下回り続けた.

        ``since``（``time.monotonic()`` の時刻）より後のサンプルで見る.
        """
        with self._lock:
            return self._tracker.released(since, time.monotonic())

    def pause_auto_tare(self, paused: bool) -> None:
        """自動のタールを止める・再び許す（接触保持の間は止める）."""
        with self._lock:
            self._paused = paused

    def tare(self) -> None:
        """今の荷重を 0 にする.

        Raises:
            ValueError: 値が来ていない、または誤りが出ている
        """
        with self._lock:
            counts = self._tracker.tare(time.monotonic())
            self._on_tare(counts)
        _logger.info("ロードセルをタールした: %d counts", counts)

    def note_z_motion(self) -> None:
        """Z を動かす指令を送った（ここから 1 s は自動のタールをしない）."""
        with self._lock:
            self._z_moved_at = time.monotonic()

    def _query(self) -> None:
        """``query_hx71x`` を送る（``_lock`` を持って呼ぶ）."""
        self._transport.send("query_hx71x", oid=self._oid, rest_ticks=self._rest_ticks)
        self._expected_sequence = 0
        self._tracker.note_start(time.monotonic())

    # 以下、受信スレッド

    def _on_data(self, message: Message, received: float) -> None:
        sequence = message.params["sequence"]
        data = message.params["data"]
        if not isinstance(sequence, int) or not isinstance(data, bytes):
            return
        samples = decode_samples(data)
        valid: list[int] = []
        for sample in samples:
            if isinstance(sample, SampleError):
                break
            valid.append(sample)
        with self._lock:
            if sequence != self._expected_sequence:
                _logger.debug(
                    "sensor_bulk_data が欠けた（%d を待って %d を受けた）",
                    self._expected_sequence,
                    sequence,
                )
                self._tracker.note_gap()
            self._expected_sequence = (sequence + 1) & _SEQUENCE_MASK
            if valid:
                # 正しいサンプルの最後は、誤りの数だけ前に読んだもの
                last = (
                    received - (len(samples) - len(valid)) / self._settings.sample_rate
                )
                first = last - (len(valid) - 1) / self._settings.sample_rate
                quiet = first - self._z_moved_at >= _QUIET_AFTER_MOTION
                counts = self._tracker.feed(
                    valid, last, quiet=quiet, auto_tare=not self._paused
                )
                if counts is not None:
                    self._on_tare(counts)
                    _logger.debug("ロードセルをタールした: %d counts", counts)
                section = self._tracker.take_section()
                if section is not None:
                    _logger.info(
                        "ロードセルの荷重（Z が%s %.1f s）: 最小 %.2f g、最大 %.2f g",
                        "止まっていた" if section.quiet else "動いていた",
                        section.seconds,
                        section.low,
                        section.high,
                    )
            if len(valid) < len(samples):
                error = samples[len(valid)]
                assert isinstance(error, SampleError)
                self._tracker.note_error(error, received)
                if received - self._requeried_at >= _REQUERY_INTERVAL:
                    _logger.warning("%s。HX711 を読み直す", error.message)
                    self._requeried_at = received
                    self._query()


def _mean(values: Sequence[int]) -> float:
    return sum(values) / len(values)
