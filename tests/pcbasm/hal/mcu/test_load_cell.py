"""`pcbasm.hal.mcu.load_cell` の仕様テスト.

- :func:`decode_samples` と :class:`LoadTracker` は時刻を引数で受ける純粋なロジックとして、値を与えて確かめる
- :class:`LoadCell` は疑似端末の向こうの MCU 役（``tests/support/wire_mcu.py``。HX711 を 80 SPS で模擬し、
  12 個ずつ ``sensor_bulk_data`` で送る）と、実物の pyserial で話す
"""

import time
from collections.abc import Iterator

import attrs
import pytest

from pcbasm.hal.mcu.firmware import PnpMotionFirmware, open_firmware
from pcbasm.hal.mcu.load_cell import (
    LoadCell,
    LoadReading,
    LoadSection,
    LoadTracker,
    SampleError,
    decode_samples,
)
from pcbasm.hal.mcu.settings import LoadCellSettings, McuMotionSettings
from tests.support.polling import wait_until
from tests.support.wire_mcu import WireMcu

_COUNTS_PER_GRAM = 1000.0
_RATE = 80
_PERIOD = 1 / _RATE


def _settings(**values: object) -> LoadCellSettings:
    defaults: dict = {
        "sclk_pin": "gpio0",
        "dout_pin": "gpio1",
        "gain_channel": 1,
        "sample_rate": _RATE,
        "counts_per_gram": _COUNTS_PER_GRAM,
    }
    return LoadCellSettings(**{**defaults, **values})


def _le(*values: int) -> bytes:
    return b"".join((value & 0xFFFFFFFF).to_bytes(4, "little") for value in values)


class _Feeder:
    """サンプルを 1 通 12 個ずつ、``rate`` [SPS] の時刻で :class:`LoadTracker` に渡す."""

    def __init__(self, tracker: LoadTracker, rate: int = _RATE) -> None:
        self.tracker = tracker
        self.now = 0.0
        self._period = 1 / rate

    def feed(
        self, value: int, count: int, *, quiet: bool = True, free: bool = False
    ) -> list[int]:
        """``value`` を ``count`` 個送り、取り直したタール値を返す."""
        changes = []
        for start in range(0, count, 12):
            batch = [value] * min(12, count - start)
            self.now += len(batch) * self._period
            changed = self.tracker.feed(batch, self.now, quiet=quiet, free=free)
            if changed is not None:
                changes.append(changed)
        return changes

    def reading(self) -> LoadReading:
        return self.tracker.reading(self.now)


def _tared(value: int = 0, **settings: object) -> _Feeder:
    feeder = _Feeder(LoadTracker(_settings(**settings)))
    feeder.feed(value, 16)
    return feeder


class TestDecodeSamples:
    def test_reads_signed_little_endian_values(self):
        data = _le(1234, -1234, 0x7FFFFF, -0x800000, 0)

        assert decode_samples(data) == [1234, -1234, 0x7FFFFF, -0x800000, 0]

    @pytest.mark.parametrize(
        ("raw", "error"),
        [(1 << 31, SampleError.DESYNC), (1 << 30, SampleError.READ_TOO_LONG)],
    )
    def test_reads_the_error_values(self, raw: int, error: SampleError):
        assert decode_samples(_le(5, raw)) == [5, error]

    def test_empty_data_has_no_samples(self):
        assert decode_samples(b"") == []

    def test_rejects_a_partial_sample(self):
        with pytest.raises(ValueError):
            decode_samples(b"\x00\x01\x02")


class TestLoadCellSettings:
    @pytest.mark.parametrize("gain_channel", [1, 2, 3])
    def test_accepts_the_hx711_gain_channels(self, gain_channel: int):
        assert _settings(gain_channel=gain_channel).gain_channel == gain_channel

    @pytest.mark.parametrize(
        "values",
        [
            {"gain_channel": 0},
            {"gain_channel": 4},
            {"sample_rate": 0},
            {"counts_per_gram": 0.0},
            {"counts_per_gram": -1.0},
            {"free_tare_zone": -0.1},
        ],
        ids=repr,
    )
    def test_rejects_values_the_firmware_or_the_conversion_cannot_use(
        self, values: dict
    ):
        with pytest.raises(ValueError):
            _settings(**values)


class TestInitialTare:
    def test_is_the_mean_of_the_first_16_samples(self):
        feeder = _Feeder(LoadTracker(_settings()))

        assert feeder.feed(1000, 12) == []
        assert feeder.reading() == LoadReading(None)
        assert feeder.feed(1000, 12) == [1000]

        assert feeder.reading() == LoadReading(0.0)

    def test_does_not_wait_for_quiet_samples(self):
        feeder = _Feeder(LoadTracker(_settings()))

        assert feeder.feed(500, 16, quiet=False) == [500]


class TestGrams:
    def test_divides_the_change_from_the_tare_by_counts_per_gram(self):
        feeder = _tared(1000)

        feeder.feed(1000 + 12_500, 12, quiet=False)

        assert feeder.reading() == LoadReading(12.5)

    def test_inverted_turns_the_sign(self):
        feeder = _tared(1000, inverted=True)

        feeder.feed(1000 - 12_500, 12, quiet=False)

        assert feeder.reading() == LoadReading(12.5)


class TestAutoTare:
    def test_takes_the_mean_of_a_quiet_and_steady_second(self):
        feeder = _tared(0)

        # 1 s（80 個）を待たずには取り直さない
        assert feeder.feed(300, 72) == []
        assert feeder.feed(300, 12) == [300]

        assert feeder.reading() == LoadReading(0.0)

    def test_retakes_about_every_second_while_quiet(self):
        feeder = _tared(0)

        assert feeder.feed(300, 80) == [300]
        assert feeder.feed(310, 80) == [310]

    def test_does_not_tare_while_z_moves(self):
        feeder = _tared(0)

        assert feeder.feed(300, 240, quiet=False) == []

        assert feeder.reading() == LoadReading(0.3)

    def test_a_moving_sample_restarts_the_second(self):
        feeder = _tared(0)

        assert feeder.feed(300, 60) == []
        assert feeder.feed(300, 12, quiet=False) == []
        assert feeder.feed(300, 72) == []
        assert feeder.feed(300, 12) == [300]

    def test_does_not_tare_when_the_spread_is_over_half_a_gram(self):
        feeder = _tared(0)
        tracker = feeder.tracker

        # 0.6 g の幅で揺れる 2 s
        for _ in range(14):
            feeder.now += 12 * _PERIOD
            assert tracker.feed([0, 600] * 6, feeder.now, quiet=True) is None

    def test_accepts_a_spread_of_half_a_gram(self):
        feeder = _tared(0)
        tracker = feeder.tracker
        changes = []

        for _ in range(7):
            feeder.now += 12 * _PERIOD
            changes.append(tracker.feed([0, 500] * 6, feeder.now, quiet=True))

        assert 250 in changes

    def test_follows_a_drift_of_up_to_two_grams(self):
        feeder = _tared(0)

        assert feeder.feed(2000, 80) == [2000]

    def test_does_not_take_in_a_steady_load_over_two_grams(self):
        # 押し続けた荷重や載せたものは、止まって安定していてもタールにしない
        feeder = _tared(0)

        assert feeder.feed(20_000, 400) == []

        assert feeder.reading() == LoadReading(20.0)

    def test_a_gap_drops_the_samples_so_far(self):
        feeder = _tared(0)

        assert feeder.feed(300, 72) == []
        feeder.tracker.note_gap()
        assert feeder.feed(300, 12) == []
        assert feeder.feed(300, 72) == [300]


class TestManualTare:
    def test_zeroes_the_mean_of_the_last_quarter_second(self):
        feeder = _tared(0)
        feeder.feed(9000, 36, quiet=False)

        assert feeder.tracker.tare(feeder.now) == 9000

        assert feeder.reading() == LoadReading(0.0)

    def test_works_before_the_initial_tare(self):
        feeder = _Feeder(LoadTracker(_settings()))
        feeder.feed(700, 12)

        assert feeder.tracker.tare(feeder.now) == 700
        assert feeder.reading() == LoadReading(0.0)

    def test_needs_a_sample_within_half_a_second(self):
        feeder = _tared(0)

        with pytest.raises(ValueError):
            feeder.tracker.tare(feeder.now + 0.6)

    def test_needs_a_sample(self):
        tracker = LoadTracker(_settings())

        with pytest.raises(ValueError):
            tracker.tare(0.0)

    def test_is_refused_while_the_samples_are_errors(self):
        feeder = _tared(0)
        feeder.tracker.note_error(SampleError.DESYNC, feeder.now)

        with pytest.raises(ValueError, match="DESYNC"):
            feeder.tracker.tare(feeder.now)


class TestSlowSampleRate:
    """10 SPS では 12 サンプルの 1 通が 1.2 s おきに届く."""

    def test_the_wait_between_messages_is_not_an_error(self):
        feeder = _Feeder(LoadTracker(_settings(sample_rate=10)), rate=10)
        feeder.feed(0, 24)
        feeder.feed(1000, 12, quiet=False)

        # 次の 1 通が届く直前
        assert feeder.tracker.reading(feeder.now + 1.19) == LoadReading(1.0)

    def test_manual_tare_works_between_messages(self):
        feeder = _Feeder(LoadTracker(_settings(sample_rate=10)), rate=10)
        feeder.feed(0, 24)
        feeder.feed(1000, 12, quiet=False)

        assert feeder.tracker.tare(feeder.now + 1.19) == 1000

    def test_two_missed_messages_are_an_error(self):
        feeder = _Feeder(LoadTracker(_settings(sample_rate=10)), rate=10)
        feeder.feed(0, 24)

        assert (
            feeder.tracker.reading(feeder.now + 2.5).error == "ロードセルの値が来ない"
        )
        with pytest.raises(ValueError):
            feeder.tracker.tare(feeder.now + 2.5)


def _periods(section: LoadSection | None) -> tuple[bool, int, float, float]:
    """区間を、長さをサンプルの周期の数にした組にする."""
    assert section is not None
    return (section.quiet, round(section.seconds / _PERIOD), section.low, section.high)


class TestSections:
    def test_reports_the_range_of_a_moving_section_when_z_stops(self):
        feeder = _tared(0)
        tracker = feeder.tracker

        feeder.feed(0, 12)
        assert tracker.take_section() is None
        for value in (-1500, 4000, 500):
            feeder.feed(value, 12, quiet=False)
        assert _periods(tracker.take_section()) == (True, 11, 0.0, 0.0)
        assert tracker.take_section() is None

        feeder.feed(0, 12)

        assert _periods(tracker.take_section()) == (False, 35, -1.5, 4.0)

    def test_a_section_that_has_not_ended_is_not_reported(self):
        feeder = _tared(0)

        feeder.feed(3000, 240, quiet=False)

        assert feeder.tracker.take_section() is None

    def test_an_error_drops_the_section(self):
        feeder = _tared(0)
        feeder.feed(3000, 12, quiet=False)

        feeder.tracker.note_error(SampleError.DESYNC, feeder.now)
        feeder.feed(0, 12)
        feeder.feed(0, 12, quiet=False)

        assert _periods(feeder.tracker.take_section()) == (True, 11, 0.0, 0.0)


class TestReadingErrors:
    def test_an_error_sample_hides_the_load_until_a_good_one(self):
        feeder = _tared(0)

        feeder.tracker.note_error(SampleError.READ_TOO_LONG, feeder.now)
        reading = feeder.reading()
        assert reading.grams is None
        assert reading.error is not None and "READ_TOO_LONG" in reading.error

        feeder.feed(2000, 12)
        assert feeder.reading() == LoadReading(2.0)

    def test_no_sample_for_half_a_second_is_an_error(self):
        feeder = _tared(0)

        reading = feeder.tracker.reading(feeder.now + 0.6)

        assert reading.grams is None
        assert reading.error == "ロードセルの値が来ない"

    def test_counts_the_wait_from_the_start(self):
        tracker = LoadTracker(_settings())
        tracker.note_start(10.0)

        assert tracker.reading(10.4) == LoadReading(None)
        assert tracker.reading(10.6).error == "ロードセルの値が来ない"


class TestFreeTare:
    """Z がピンセットから離れた範囲にあるときは、差の大きさを問わずタールに取り込む."""

    def test_takes_in_a_load_far_over_the_drift_limit(self):
        feeder = _tared(0)

        assert feeder.feed(30_000, 80, free=True) == [30_000]

        assert feeder.reading() == LoadReading(0.0)

    def test_outside_the_zone_the_drift_limit_stays(self):
        feeder = _tared(0)

        assert feeder.feed(30_000, 240, free=False) == []

        assert feeder.reading() == LoadReading(30.0)

    def test_still_needs_a_quiet_and_steady_second(self):
        feeder = _tared(0)

        assert feeder.feed(30_000, 240, quiet=False, free=True) == []
        for _ in range(14):
            feeder.now += 12 * _PERIOD
            # 0.6 g の幅で揺れる
            assert (
                feeder.tracker.feed(
                    [30_000, 30_600] * 6, feeder.now, quiet=True, free=True
                )
                is None
            )

    def test_does_not_tare_while_paused(self):
        feeder = _tared(0)

        for _ in range(20):
            feeder.now += 12 * _PERIOD
            assert (
                feeder.tracker.feed(
                    [30_000] * 12, feeder.now, quiet=True, auto_tare=False, free=True
                )
                is None
            )


class TestPausedAutoTare:
    def test_does_not_tare_while_paused_even_if_quiet(self):
        feeder = _tared(0)

        for _ in range(20):
            feeder.now += 12 * _PERIOD
            assert (
                feeder.tracker.feed([300] * 12, feeder.now, quiet=True, auto_tare=False)
                is None
            )

        assert feeder.reading() == LoadReading(0.3)


class TestReady:
    def test_needs_the_tare(self):
        feeder = _Feeder(LoadTracker(_settings()))
        feeder.feed(0, 12)

        assert not feeder.tracker.ready(feeder.now)
        feeder.feed(0, 4)
        assert feeder.tracker.ready(feeder.now)

    def test_is_false_when_the_last_sample_is_old_or_an_error(self):
        feeder = _tared(0)

        assert not feeder.tracker.ready(feeder.now + 0.6)
        feeder.tracker.note_error(SampleError.DESYNC, feeder.now)
        assert not feeder.tracker.ready(feeder.now)


class TestReleased:
    """接触保持から戻るのは、保持に入った後の連続 100 ms（80 SPS で 8 個）が release_force（15
    g）を下回ったとき."""

    def test_needs_eight_samples_below_the_release_force_after_since(self):
        feeder = _tared(0)
        since = feeder.now

        feeder.feed(14_000, 7)
        assert not feeder.tracker.released(since, feeder.now)
        feeder.feed(14_000, 1)
        assert feeder.tracker.released(since, feeder.now)

    def test_samples_before_since_do_not_count(self):
        feeder = _tared(0)
        feeder.feed(0, 12)

        assert not feeder.tracker.released(feeder.now, feeder.now)

    def test_a_sample_at_the_release_force_keeps_holding(self):
        feeder = _tared(0)
        since = feeder.now

        feeder.feed(0, 7)
        feeder.feed(15_000, 1)

        assert not feeder.tracker.released(since, feeder.now)

    def test_only_the_last_hundred_milliseconds_count(self):
        feeder = _tared(0)
        since = feeder.now

        feeder.feed(60_000, 4)
        feeder.feed(0, 8)

        assert feeder.tracker.released(since, feeder.now)

    def test_an_error_or_an_old_sample_is_not_released(self):
        feeder = _tared(0)
        since = feeder.now
        feeder.feed(0, 12)

        assert not feeder.tracker.released(since, feeder.now + 0.6)
        feeder.tracker.note_error(SampleError.DESYNC, feeder.now)
        assert not feeder.tracker.released(since, feeder.now)


class TestMean:
    """校正の平均: 数え始めてから届いたサンプルを ``count`` 個そろえ、今のタール値との差 [counts] を返す."""

    def test_averages_the_samples_after_the_begin_minus_the_tare(self):
        feeder = _tared(1000)
        feeder.feed(9000, 12, quiet=False)  # 数え始める前のサンプルは数えない

        feeder.tracker.begin_mean(16)
        feeder.feed(3000, 8, quiet=False)
        feeder.feed(5000, 12, quiet=False)  # 16 個を超えた分は数えない

        assert feeder.tracker.mean() == 3000.0

    def test_is_none_until_the_count_is_reached(self):
        feeder = _tared(0)
        feeder.tracker.begin_mean(16)

        feeder.feed(3000, 15, quiet=False)
        assert feeder.tracker.mean() is None
        feeder.feed(3000, 1, quiet=False)
        assert feeder.tracker.mean() == 3000.0

    def test_inverted_counts_the_pressing_direction_as_positive(self):
        feeder = _tared(1000, inverted=True)
        feeder.tracker.begin_mean(12)

        feeder.feed(-2000, 12, quiet=False)

        assert feeder.tracker.mean() == 3000.0

    def test_an_error_sample_while_counting_is_refused(self):
        feeder = _tared(0)
        feeder.tracker.begin_mean(16)
        feeder.feed(3000, 8, quiet=False)

        feeder.tracker.note_error(SampleError.READ_TOO_LONG, feeder.now)
        feeder.feed(3000, 12, quiet=False)

        with pytest.raises(ValueError, match="READ_TOO_LONG"):
            feeder.tracker.mean()


# --- MCU 役と話す ---


@pytest.fixture
def load_settings(settings: McuMotionSettings) -> McuMotionSettings:
    return attrs.evolve(
        settings,
        led_pin="gpio17",
        load_cell=_settings(counts_per_gram=100.0),
    )


@pytest.fixture
def firmware(load_settings: McuMotionSettings) -> Iterator[PnpMotionFirmware]:
    opened = open_firmware(load_settings)
    yield opened
    opened.close()


def _load_cell(firmware: PnpMotionFirmware) -> LoadCell:
    load_cell = firmware.load_cell
    assert load_cell is not None
    return load_cell


def _keep_z_moving(load_cell: LoadCell, seconds: float) -> None:
    """Z を動かし続けたことにして（自動のタールを止めて）``seconds`` 秒待つ."""
    end = time.monotonic() + seconds

    def waited() -> bool:
        load_cell.note_z_motion()
        return time.monotonic() >= end

    wait_until(waited, what=f"{seconds} 秒")


def _queries(mcu: WireMcu) -> list[int]:
    return [
        params["rest_ticks"] for name, params in mcu.accepted() if name == "query_hx71x"
    ]


class TestLoadCellOverTheWire:
    def test_reads_grams_from_the_tare_at_the_start(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        load_cell = _load_cell(firmware)
        wait_until(lambda: load_cell.reading() == LoadReading(0.0), what="タール")

        mcu.control("load", counts=1250)

        wait_until(lambda: load_cell.reading() == LoadReading(12.5), what="12.5 g")

    def test_manual_tare_zeroes_the_load(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        load_cell = _load_cell(firmware)
        wait_until(lambda: load_cell.reading() == LoadReading(0.0), what="タール")
        mcu.control("load", counts=1250)
        # 手のタールは直近 0.25 s の平均なので、押した値だけになるまで待つ（その間、自動では取らせない）
        _keep_z_moving(load_cell, 0.6)
        assert load_cell.reading() == LoadReading(12.5)

        load_cell.tare()

        assert load_cell.reading() == LoadReading(0.0)

    def test_an_error_sample_is_shown_and_recovered_by_querying_again(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        load_cell = _load_cell(firmware)
        wait_until(lambda: load_cell.reading() == LoadReading(0.0), what="タール")
        before = len(_queries(mcu))

        mcu.control("hx71x_error")

        # 誤りを受けると query_hx71x を送り直し、MCU 役はそれで読み直す
        wait_until(lambda: len(_queries(mcu)) > before, what="query_hx71x の送り直し")
        wait_until(lambda: load_cell.reading() == LoadReading(0.0), what="回復")

    def test_a_skipped_sequence_keeps_reading(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        load_cell = _load_cell(firmware)
        wait_until(lambda: load_cell.reading() == LoadReading(0.0), what="タール")

        mcu.control("hx71x_skip")
        mcu.control("load", counts=-500)

        wait_until(lambda: load_cell.reading() == LoadReading(-5.0), what="-5 g")

    def test_samples_that_stop_coming_are_an_error(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        load_cell = _load_cell(firmware)
        wait_until(lambda: load_cell.reading() == LoadReading(0.0), what="タール")

        mcu.control("shutdown", reason=5)

        wait_until(
            lambda: load_cell.reading().error == "ロードセルの値が来ない",
            what="値が来ないこと",
        )

    def test_tare_without_samples_is_refused(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        load_cell = _load_cell(firmware)
        mcu.control("shutdown", reason=5)
        wait_until(lambda: load_cell.reading().error is not None, what="値が来ないこと")

        with pytest.raises(ValueError):
            load_cell.tare()

    def test_mean_counts_is_the_mean_from_the_tare(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        load_cell = _load_cell(firmware)
        wait_until(lambda: load_cell.reading() == LoadReading(0.0), what="タール")
        mcu.control("load", counts=1250)
        wait_until(lambda: load_cell.reading() == LoadReading(12.5), what="12.5 g")

        assert load_cell.mean_counts(0.25) == 1250.0

    def test_mean_counts_without_samples_is_refused(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        load_cell = _load_cell(firmware)
        wait_until(lambda: load_cell.reading() == LoadReading(0.0), what="タール")
        mcu.control("shutdown", reason=5)

        with pytest.raises(ValueError, match="来ない"):
            load_cell.mean_counts(0.1)

    def test_mean_counts_with_an_error_sample_is_refused(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        load_cell = _load_cell(firmware)
        wait_until(lambda: load_cell.reading() == LoadReading(0.0), what="タール")
        # 送り直した query_hx71x で回復しないよう、MCU 役の応答を止めてから乱す
        mcu.control("drop_blocks", count=1000)
        mcu.control("hx71x_error")

        with pytest.raises(ValueError):
            load_cell.mean_counts(1.0)
