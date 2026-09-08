"""実機 latency 計測の公開契約.

計画 §4.4 / §6.8 に対応する。

実機でしか意味を持たない数値ではなく、計測の機構を学習機で確かめる。
"""

from __future__ import annotations

import resource
from pathlib import Path

import attrs
import numpy as np
import pytest

from ml.export.benchmark import (
    BenchmarkCase,
    ColdStartMeasurement,
    DeviceBenchmark,
    LatencyStatistics,
)
from tests.ml.export import support

DEVICE_LABEL = "workstation-cpu"

# 1 ms 刻みの 20 点。percentile の定義（nearest-rank / 線形補間）の差より、index を
# 1 つずらした誤りの方が大きくなる並び
DURATIONS = tuple(0.001 * (index + 1) for index in range(20))


def _cases(directory: Path, count: int = 2) -> tuple[BenchmarkCase, ...]:
    cases: list[BenchmarkCase] = []
    for index in range(count):
        case, error = BenchmarkCase.write(
            directory,
            case_id=f"case-{index:03d}",
            values=support.input_values(height=32 + 16 * index, seed=index),
        )
        assert case is not None, error
        cases.append(case)
    return tuple(cases)


def _package(tmp_path: Path) -> Path:
    return support.publish_model_package(tmp_path / "package").path


def _measure(tmp_path: Path, *, case_count: int = 2) -> DeviceBenchmark:
    benchmark, error = DeviceBenchmark.measure(
        _package(tmp_path),
        _cases(tmp_path / "cases", case_count),
        device_label=DEVICE_LABEL,
        warmup_count=2,
        measured_count=5,
    )

    assert benchmark is not None, error
    return benchmark


class TestLatencyStatistics:
    """計測した所要時間の要約."""

    def test_summarizes_a_known_series(self):
        statistics, error = LatencyStatistics.of(DURATIONS)

        assert error is None
        assert statistics is not None
        assert statistics.measured_count == 20
        assert statistics.minimum_seconds == pytest.approx(0.001)
        assert statistics.maximum_seconds == pytest.approx(0.020)
        # 許容幅は percentile の定義差だけを吸収する。1 点ずらすと外れる
        assert statistics.p50_seconds == pytest.approx(0.01025, abs=3e-4)
        assert statistics.p95_seconds == pytest.approx(0.019, abs=3e-4)
        assert statistics.p99_seconds == pytest.approx(0.0199, abs=2e-4)

    def test_does_not_depend_on_the_order_of_the_series(self):
        forward, _ = LatencyStatistics.of(DURATIONS)
        backward, _ = LatencyStatistics.of(tuple(reversed(DURATIONS)))

        assert forward == backward

    def test_reports_an_empty_series(self):
        statistics, error = LatencyStatistics.of(())

        assert statistics is None
        assert error is not None

    @pytest.mark.parametrize(
        "durations",
        [
            pytest.param((0.001, float("nan")), id="not-a-number"),
            pytest.param((0.001, float("inf")), id="infinite"),
            pytest.param((0.001, -0.002), id="negative"),
        ],
    )
    def test_reports_a_duration_that_cannot_be_measured(
        self, durations: tuple[float, ...]
    ):
        statistics, error = LatencyStatistics.of(durations)

        assert statistics is None
        assert error is not None


class TestBenchmarkCase:
    """子プロセスへ渡す入力を file 経由で受け渡す."""

    def test_round_trips_the_input_values(self, tmp_path: Path):
        values = support.input_values(seed=3)
        case, error = BenchmarkCase.write(tmp_path, case_id="case-000", values=values)

        assert case is not None, error
        loaded, error = case.load_values()

        assert error is None
        assert loaded is not None
        assert sorted(loaded) == sorted(values)
        assert all(np.array_equal(loaded[name], values[name]) for name in values)

    def test_writes_the_input_file_inside_the_given_directory(self, tmp_path: Path):
        case = _cases(tmp_path)[0]

        assert case.input_path.is_file()
        assert case.input_path.parent == tmp_path
        assert case.validate() is None

    def test_reports_an_empty_case_id(self, tmp_path: Path):
        case, error = BenchmarkCase.write(
            tmp_path, case_id="", values=support.input_values()
        )

        assert case is None
        assert error is not None

    def test_reports_a_case_without_any_value(self, tmp_path: Path):
        case, error = BenchmarkCase.write(tmp_path, case_id="case-000", values={})

        assert case is None
        assert error is not None

    def test_reports_an_input_file_that_disappeared(self, tmp_path: Path):
        case = _cases(tmp_path)[0]
        case.input_path.unlink()

        loaded, error = case.load_values()

        assert loaded is None
        assert error is not None

    def test_reports_a_case_id_that_is_not_a_plain_filename(self, tmp_path: Path):
        case, error = BenchmarkCase.write(
            tmp_path, case_id="cases/case-000", values=support.input_values()
        )

        assert case is None
        assert error is not None

    @pytest.mark.parametrize(
        ("case_id", "written"),
        [
            pytest.param("", True, id="empty-case-id"),
            pytest.param("cases/case-000", True, id="case-id-with-a-directory"),
            pytest.param(".hidden", True, id="hidden-case-id"),
            pytest.param("case-000", False, id="input-file-is-missing"),
        ],
    )
    def test_reports_a_malformed_case(
        self, tmp_path: Path, case_id: str, written: bool
    ):
        # BenchmarkCase.write を通さずに組める。measure の入口はこれを検査する
        case = BenchmarkCase(case_id=case_id, input_path=tmp_path / "absent.npz")
        if written:
            case.input_path.write_bytes(b"")

        assert case.validate() is not None


class TestColdStartMeasurement:
    """Process 起動から初回予測までを子プロセスで測る."""

    def test_measures_a_child_process_that_never_loads_torch(self, tmp_path: Path):
        package = _package(tmp_path)
        case = _cases(tmp_path / "cases", 1)[0]

        measurement, error = ColdStartMeasurement.measure(package, case)

        assert measurement is not None, error
        assert measurement.elapsed_seconds > 0.0
        assert measurement.peak_resident_kibibytes > 0
        # 親は torch を読み込み済み。子が親の high-water mark を継承していたら等しくなる
        assert (
            measurement.peak_resident_kibibytes
            < resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        )

    def test_reports_a_package_that_cannot_be_loaded(self, tmp_path: Path):
        case = _cases(tmp_path / "cases", 1)[0]

        measurement, error = ColdStartMeasurement.measure(tmp_path / "absent", case)

        assert measurement is None
        assert error is not None
        # 子プロセスの stdout が空なら JSON 解析でも理由は返る。終了コードを
        # 見ていることまで固定する
        assert "cold start の計測に失敗しました" in error

    def test_reports_a_case_it_cannot_use(self, tmp_path: Path):
        case = BenchmarkCase(case_id="absent", input_path=tmp_path / "absent.npz")

        measurement, error = ColdStartMeasurement.measure(_package(tmp_path), case)

        assert measurement is None
        assert error is not None
        assert "case の入力ファイルがありません" in error


class TestDeviceBenchmark:
    """1 機体分の計測をまとめる."""

    def test_measures_every_case(self, tmp_path: Path):
        benchmark = _measure(tmp_path)

        assert tuple(case.case_id for case in benchmark.cases) == (
            "case-000",
            "case-001",
        )
        assert all(case.statistics.measured_count == 5 for case in benchmark.cases)
        assert benchmark.device_label == DEVICE_LABEL
        assert benchmark.validate() is None

    def test_reports_the_worst_p95_across_the_cases(self, tmp_path: Path):
        benchmark = _measure(tmp_path)

        assert benchmark.worst_p95_seconds == max(
            case.statistics.p95_seconds for case in benchmark.cases
        )

    def test_counts_every_payload_in_the_artifact_size(self, tmp_path: Path):
        benchmark = _measure(tmp_path)
        package = tmp_path / "package"
        payloads = sorted(entry for entry in package.iterdir())

        assert (
            benchmark.artifact_bytes > (package / support.MODEL_FILENAME).stat().st_size
        )
        assert benchmark.artifact_bytes <= sum(
            entry.stat().st_size for entry in payloads
        )

    def test_measures_the_cold_start_in_a_child_process(self, tmp_path: Path):
        benchmark = _measure(tmp_path, case_count=1)

        assert benchmark.cold_start_seconds > 0.0
        assert benchmark.peak_resident_kibibytes > 0
        assert (
            benchmark.peak_resident_kibibytes
            < resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        )

    def test_converts_itself_into_promotion_evidence(self, tmp_path: Path):
        benchmark = _measure(tmp_path)

        evidence = benchmark.as_latency_evidence()

        assert evidence.device_label == DEVICE_LABEL
        assert evidence.worst_p95_seconds == benchmark.worst_p95_seconds
        assert evidence.cold_start_seconds == benchmark.cold_start_seconds
        assert evidence.validate() is None

    def test_round_trips_through_a_document(self, tmp_path: Path):
        benchmark = _measure(tmp_path)

        benchmark.save(tmp_path / "benchmark.json")
        loaded, error = DeviceBenchmark.load(tmp_path / "benchmark.json")

        assert error is None
        assert loaded == benchmark

    @pytest.mark.parametrize(
        ("warmup_count", "measured_count", "case_count"),
        [
            pytest.param(-1, 5, 1, id="negative-warmup"),
            pytest.param(2, 0, 1, id="no-measurement"),
            pytest.param(2, 5, 0, id="no-case"),
        ],
    )
    def test_reports_arguments_that_cannot_be_measured(
        self, tmp_path: Path, warmup_count: int, measured_count: int, case_count: int
    ):
        benchmark, error = DeviceBenchmark.measure(
            _package(tmp_path),
            _cases(tmp_path / "cases", case_count),
            device_label=DEVICE_LABEL,
            warmup_count=warmup_count,
            measured_count=measured_count,
        )

        assert benchmark is None
        assert error is not None

    def test_reports_a_later_case_whose_input_file_is_missing(self, tmp_path: Path):
        # cold start は cases[0] しか検査しないので、2 件目の不備を捕まえるのは
        # measure の入口検査だけ
        first, second = _cases(tmp_path / "cases", 2)
        second.input_path.unlink()

        benchmark, error = DeviceBenchmark.measure(
            _package(tmp_path),
            (first, second),
            device_label=DEVICE_LABEL,
            warmup_count=2,
            measured_count=5,
        )

        assert benchmark is None
        assert error is not None
        assert "case の入力ファイルがありません" in error

    def test_reports_a_later_case_whose_input_file_is_not_an_archive(
        self, tmp_path: Path
    ):
        # cold start は cases[0] しか読まず、入口検査は is_file() しか見ない。
        # 2 件目の壊れた .npz は計測 loop の入力読み込みまで届く
        first, second = _cases(tmp_path / "cases", 2)
        second.input_path.write_bytes(b"not an npz archive")

        benchmark, error = DeviceBenchmark.measure(
            _package(tmp_path),
            (first, second),
            device_label=DEVICE_LABEL,
            warmup_count=2,
            measured_count=5,
        )

        assert benchmark is None
        assert error is not None
        assert "case の入力を読めません" in error

    def test_reports_a_case_whose_input_file_is_not_an_archive(self, tmp_path: Path):
        # 親は package を読めるが、子プロセスが np.load で落ちる
        case = _cases(tmp_path / "cases", 1)[0]
        case.input_path.write_bytes(b"not an npz archive")

        benchmark, error = DeviceBenchmark.measure(
            _package(tmp_path),
            (case,),
            device_label=DEVICE_LABEL,
            warmup_count=2,
            measured_count=5,
        )

        assert benchmark is None
        assert error is not None
        assert "cold start の計測に失敗しました" in error

    @pytest.mark.parametrize(
        "warmup_count",
        [
            # warm-up は結果を捨てるので、warm-up 中に失敗しても握り潰されず
            # 計測 loop が同じ理由を返すことを固定する
            pytest.param(2, id="with-warm-up"),
            pytest.param(0, id="without-warm-up"),
        ],
    )
    def test_reports_a_case_the_model_cannot_run(
        self, tmp_path: Path, warmup_count: int
    ):
        # cold start が通る case を先頭に置き、model が受け取れない case を続ける
        measurable = _cases(tmp_path / "cases", 1)[0]
        unusable, error = BenchmarkCase.write(
            tmp_path / "cases",
            case_id="wrong-inputs",
            values={"unexpected_input": np.zeros((1, 1), np.float32)},
        )

        assert unusable is not None, error

        benchmark, error = DeviceBenchmark.measure(
            _package(tmp_path),
            (measurable, unusable),
            device_label=DEVICE_LABEL,
            warmup_count=warmup_count,
            measured_count=5,
        )

        assert benchmark is None
        assert error is not None
        # 入力名の食い違いは ORT ではなく ml 側の manifest 照合が弾く。
        # ORT の例外を包む "推論に失敗しました" が出ないことで層を区別する
        assert "推論に失敗しました" not in error
        assert "unexpected_input" in error

    def test_reports_a_measurement_count_below_one(self, tmp_path: Path):
        # 0 回でも LatencyStatistics.of が空列を弾くので、理由文まで見ないと
        # 入口の検査が消えても気付けない
        benchmark, error = DeviceBenchmark.measure(
            _package(tmp_path),
            _cases(tmp_path / "cases", 1),
            device_label=DEVICE_LABEL,
            warmup_count=2,
            measured_count=0,
        )

        assert benchmark is None
        assert error is not None
        assert "measured_count は 1 以上が必要です" in error

    def test_reports_a_duplicate_case_id(self, tmp_path: Path):
        case = _cases(tmp_path / "cases", 1)[0]

        benchmark, error = DeviceBenchmark.measure(
            _package(tmp_path),
            (case, case),
            device_label=DEVICE_LABEL,
            warmup_count=2,
            measured_count=5,
        )

        assert benchmark is None
        assert error is not None
        assert "case_id が重複しています" in error

    def test_reports_a_case_whose_input_file_is_missing(self, tmp_path: Path):
        case = _cases(tmp_path / "cases", 1)[0]
        case.input_path.unlink()

        benchmark, error = DeviceBenchmark.measure(
            _package(tmp_path),
            (case,),
            device_label=DEVICE_LABEL,
            warmup_count=2,
            measured_count=5,
        )

        assert benchmark is None
        assert error is not None
        assert "case の入力ファイルがありません" in error


@pytest.fixture(scope="class")
def measured(tmp_path_factory: pytest.TempPathFactory) -> DeviceBenchmark:
    """計測を 1 度だけ回して class 内で使い回す."""

    return _measure(tmp_path_factory.mktemp("validation"))


class TestDeviceBenchmarkValidation:
    """計測結果そのものが判定に使える形かの検証.

    ``load`` は構造しか見ないので、``validate()`` を呼ぶのは利用側の責任になる。
    """

    def test_accepts_a_measured_benchmark(self, measured: DeviceBenchmark):
        assert measured.validate() is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("device_label", "", id="empty-device-label"),
            pytest.param("onnxruntime_version", "", id="empty-runtime-version"),
            pytest.param("warmup_count", -1, id="negative-warmup-count"),
            pytest.param("cases", (), id="no-case"),
            pytest.param("cold_start_seconds", 0.0, id="cold-start-at-zero"),
            pytest.param(
                "cold_start_seconds", float("inf"), id="non-finite-cold-start"
            ),
            pytest.param("peak_resident_kibibytes", 0, id="peak-rss-at-zero"),
            pytest.param("artifact_bytes", 0, id="artifact-bytes-at-zero"),
        ],
    )
    def test_reports_a_malformed_benchmark(
        self, measured: DeviceBenchmark, field: str, value: object
    ):
        assert attrs.evolve(measured, **{field: value}).validate() is not None

    def test_reports_a_package_that_cannot_be_loaded(self, tmp_path: Path):
        benchmark, error = DeviceBenchmark.measure(
            tmp_path / "absent",
            _cases(tmp_path / "cases", 1),
            device_label=DEVICE_LABEL,
            warmup_count=2,
            measured_count=5,
        )

        assert benchmark is None
        assert error is not None

    def test_reports_an_empty_device_label(self, tmp_path: Path):
        benchmark, error = DeviceBenchmark.measure(
            _package(tmp_path),
            _cases(tmp_path / "cases", 1),
            device_label="",
            warmup_count=2,
            measured_count=5,
        )

        assert benchmark is None
        assert error is not None
