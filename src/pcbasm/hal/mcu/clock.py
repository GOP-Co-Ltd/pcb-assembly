"""MCU の clock とホストの時刻の対応を推定する.

仕様として読んだもの（コードは写していない。pcbasm は MIT、出典は GPLv3）:

- ``src/firmware/pnp-motion/klipper/docs/Protocol.md``（応答は送り直されないので、失われうる）
- ファームの C ソース ``klipper/src/basecmd.c``（``get_clock`` → ``clock clock=%u`` は 32 bit の clock、
  ``get_uptime`` → ``uptime high=%u clock=%u`` は 64 bit の clock）

推定の方式は次の考えから独立に導いた（Klippy の clocksync.py の方式は使っていない）。

- **標本:** ``get_clock`` を送った時刻 ``sent`` と応答を受けた時刻 ``received`` の間のどこかで、MCU は
  clock を読んだ。読んだ時刻は中点と見なし、その誤差は往復時間（RTT）の半分を超えない
- **基準:** 直近の標本のうち RTT が最小のものを基準点にする。誤差の上限がいちばん小さい標本なので、
  推定の誤差の上限もそれで決まる。平均すると、帰りだけが遅れるような偏った遅れがそのまま残る
- **周波数:** 水晶の誤差（数十 ppm）で公称の周波数からずれる。直近の標本の（中点, clock）に直線を
  最小二乗であてはめ、その傾きを使う。中点の誤差が大きい標本（RTT が最小の 2 倍に 1 ms を足したより
  大きいもの）は傾きを狂わせるのであてはめから外す。数秒に満たない区間の傾きは誤差が大きいので、
  それまでは公称の周波数を使う
- **32 bit:** ``get_clock`` の clock は 32 bit（12 MHz で約 358 秒で折り返す）。予測した 64 bit の値に
  いちばん近い値に伸ばす。標本は 1 秒ごとに取るので、折り返しの半分（約 179 秒）より十分短い
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import attrs

from pcbasm.hal.mcu.dictionary import Message
from pcbasm.hal.mcu.errors import McuError
from pcbasm.hal.mcu.transport import McuTransport

_logger = logging.getLogger(__name__)

_CLOCK32 = 2**32
_MAX_SAMPLES = 30  # 1 秒ごとなら直近の 30 秒
_MIN_FIT_SPAN = 2.0  # 傾きをあてはめる区間の最小 [s]
_RTT_SLACK = 0.001  # あてはめに使う RTT の上限 = 最小の RTT × 2 + これ [s]


def to_clock32(clock: int) -> int:
    """64 bit の clock の下位 32 bit（MCU のコマンドに載せる形）."""
    return clock % _CLOCK32


def _nearest(clock32: int, reference: int) -> int:
    """下位 32 bit が ``clock32`` の値のうち、``reference`` にいちばん近いもの."""
    return reference + (clock32 - reference + _CLOCK32 // 2) % _CLOCK32 - _CLOCK32 // 2


@attrs.frozen
class _Sample:
    midpoint: float
    rtt: float
    clock: int


class ClockEstimator:
    """ホストの時刻（``time.monotonic()`` の秒）から MCU の 64 bit の clock を推定する（純粋）."""

    def __init__(self, frequency: float) -> None:
        self._nominal = frequency
        self._frequency = frequency
        self._samples: deque[_Sample] = deque(maxlen=_MAX_SAMPLES)
        self._anchor: _Sample | None = None

    def add_sample(self, sent: float, received: float, clock: int) -> None:
        """``sent`` に送った問い合わせに、``received`` に clock が返った標本を足す.

        最初の標本の clock は 64 bit の値として使う（``get_uptime`` の応答）。2 つ目からは下位 32 bit
        だけを見て、予測に近い値に伸ばす（``get_clock`` の応答）。
        """
        midpoint = (sent + received) / 2
        if self._anchor is not None:
            clock = _nearest(to_clock32(clock), self.clock_at(midpoint))
        self._samples.append(
            _Sample(midpoint=midpoint, rtt=received - sent, clock=clock)
        )
        self._refit()

    def clock_at(self, t: float) -> int:
        """ホストの時刻 ``t`` の MCU の clock（64 bit）."""
        if self._anchor is None:
            raise McuError("MCU の clock の標本がまだ無い")
        return self._anchor.clock + round((t - self._anchor.midpoint) * self._frequency)

    def extend(self, clock32: int) -> int:
        """32 bit の clock を、最後の標本にいちばん近い 64 bit の値に伸ばす."""
        if not self._samples:
            raise McuError("MCU の clock の標本がまだ無い")
        return _nearest(clock32, self._samples[-1].clock)

    def _refit(self) -> None:
        # RTT が同じなら新しい標本を基準にする（外挿する距離が短い）
        self._anchor = min(reversed(self._samples), key=lambda sample: sample.rtt)
        limit = self._anchor.rtt * 2 + _RTT_SLACK
        fit = [sample for sample in self._samples if sample.rtt <= limit]
        span = fit[-1].midpoint - fit[0].midpoint
        if span < _MIN_FIT_SPAN:
            self._frequency = self._nominal
            return
        # 基準点からの差で計算して、64 bit の clock でも float の桁を落とさない
        xs = [sample.midpoint - self._anchor.midpoint for sample in fit]
        ys = [sample.clock - self._anchor.clock for sample in fit]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        covariance = sum(
            (x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)
        )
        variance = sum((x - mean_x) ** 2 for x in xs)
        self._frequency = covariance / variance


class ClockSync:
    """``get_clock`` を周期的に問い合わせて、MCU の clock を推定し続ける.

    :meth:`start` で ``get_uptime`` から 64 bit の起点を取り、周期のスレッド（daemon）を始める。
    :meth:`close` でスレッドを止める（transport は閉じない）。
    """

    def __init__(
        self, transport: McuTransport, frequency: float, *, interval: float = 1.0
    ) -> None:
        self._transport = transport
        self._frequency = frequency
        self._interval = interval
        self._estimator = ClockEstimator(frequency)
        self._lock = threading.Lock()
        self._outstanding: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """64 bit の起点を取り、``get_clock`` の周期の問い合わせを始める."""
        sent = time.monotonic()
        uptime = self._transport.query("get_uptime", response="uptime")
        received = time.monotonic()
        high, low = uptime.params["high"], uptime.params["clock"]
        assert isinstance(high, int) and isinstance(low, int)
        with self._lock:
            self._estimator.add_sample(sent, received, (high << 32) | low)
        self._transport.subscribe("clock", self._on_clock)
        self._thread = threading.Thread(
            target=self._poll, name="mcu-clock-sync", daemon=True
        )
        self._thread.start()

    def now(self) -> int:
        """今の MCU の clock（64 bit）."""
        with self._lock:
            return self._estimator.clock_at(time.monotonic())

    def ticks(self, seconds: float) -> int:
        """秒を clock の tick 数に直す（公称の周波数）."""
        return round(seconds * self._frequency)

    def extend(self, clock32: int) -> int:
        """MCU が報告した 32 bit の clock を、今にいちばん近い 64 bit の値に伸ばす."""
        return _nearest(clock32, self.now())

    def close(self) -> None:
        """周期の問い合わせを止める."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _poll(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                self._outstanding = time.monotonic()
            try:
                self._transport.send("get_clock")
            except McuError as error:
                _logger.debug("MCU の clock の問い合わせをやめた: %s", error)
                return
            self._stop.wait(self._interval)

    def _on_clock(self, message: Message, received: float) -> None:
        clock = message.params["clock"]
        assert isinstance(clock, int)
        with self._lock:
            # 送った問い合わせ 1 つに 1 つの応答だけを対にする。応答は ack より先に届くので、接続断の期限
            # （既定 1 秒）を超えて遅れることはなく、前の周期の応答を今の周期と取り違えない
            if self._outstanding is None:
                return
            sent, self._outstanding = self._outstanding, None
            self._estimator.add_sample(sent, received, clock)
