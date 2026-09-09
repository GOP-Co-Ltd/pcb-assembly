"""実機での latency と常駐量の計測.

計測の機構だけを ``ml`` に置き、実行は運用ジョブに任せる。

Pi の benchmark に要るのは promote 済みの package であって、``ml`` 単体では
作れないため。

cold start は子プロセスで測る。

同一プロセスで測ると、既に読み込まれた module と暖まった allocator のぶんだけ
実運転より速く小さく出る。

peak RSS は子が ``/proc/self/status`` の ``VmHWM`` から読む。

``getrusage`` の ``ru_maxrss`` は親の high-water mark を引き継ぐので使えない。
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

import attrs
import numpy as np
import onnxruntime
from numpy.typing import NDArray

from ml.artifact.atomic import atomic_write_stream
from ml.artifact.document import DocumentKind
from ml.export.promotion import LatencyEvidence
from ml.export.runtime import OnnxInferenceModel
from ml.serialization import make_strict_converter

DEVICE_BENCHMARK_DOCUMENT = DocumentKind(kind="ml-device-benchmark", schema_version=1)

_CASE_SUFFIX = ".npz"
_CONVERTER = make_strict_converter()

# 子プロセスで動かす cold start 計測。``ml.export.runtime`` と numpy しか読まない。
_COLD_START_PROGRAM = """
import json
import sys
import time

started = time.perf_counter()

import numpy as np

from ml.export.runtime import OnnxInferenceModel

model, error = OnnxInferenceModel.load(sys.argv[1])
if model is None:
    sys.exit(error)
with np.load(sys.argv[2]) as archive:
    inputs = {name: archive[name] for name in archive.files}
outputs, error = model.predict(inputs)
if outputs is None:
    sys.exit(error)
elapsed = time.perf_counter() - started

peak = 0
with open("/proc/self/status", encoding="utf-8") as stream:
    for line in stream:
        if line.startswith("VmHWM:"):
            peak = int(line.split()[1])

print(json.dumps({"elapsed_seconds": elapsed, "peak_resident_kibibytes": peak}))
"""


@attrs.frozen
class LatencyStatistics:
    """1 case ぶんの latency 分布.

    百分位は nearest-rank で取る。

    補間した値は実際に観測していない数なので、実機の報告値には使わない。
    """

    measured_count: int
    p50_seconds: float
    p95_seconds: float
    p99_seconds: float
    minimum_seconds: float
    maximum_seconds: float

    @classmethod
    def of(
        cls, durations: Sequence[float]
    ) -> tuple[LatencyStatistics | None, str | None]:
        """実測した所要秒の並びから百分位を求める."""

        if not durations:
            return None, "durations は 1 件以上が必要です"
        for duration in durations:
            if not math.isfinite(duration) or duration < 0.0:
                return None, f"durations は 0 以上の有限値が必要です: {duration}"
        ordered = sorted(durations)
        return (
            cls(
                measured_count=len(ordered),
                p50_seconds=_percentile(ordered, 50),
                p95_seconds=_percentile(ordered, 95),
                p99_seconds=_percentile(ordered, 99),
                minimum_seconds=ordered[0],
                maximum_seconds=ordered[-1],
            ),
            None,
        )


@attrs.frozen(eq=False)
class BenchmarkCase:
    """1 case ぶんの入力を収めた ``.npz`` ファイル.

    In-memory の配列ではなくファイルで持つ。

    cold start を測る子プロセスへ入力を渡す手段が他に無いため。
    """

    case_id: str
    input_path: Path

    def validate(self) -> str | None:
        """Case の id と入力ファイルが使える状態かを検証する."""

        if not self.case_id:
            return "case_id は空にできません"
        if Path(self.case_id).name != self.case_id or self.case_id.startswith("."):
            return f"case_id はファイル名に使える綴りが必要です: {self.case_id!r}"
        if not self.input_path.is_file():
            return f"case の入力ファイルがありません: {self.input_path}"
        return None

    @classmethod
    def write(
        cls,
        directory: Path,
        *,
        case_id: str,
        values: Mapping[str, NDArray[np.float32]],
    ) -> tuple[BenchmarkCase | None, str | None]:
        """入力を ``.npz`` として書き出し、case を返す."""

        if not case_id:
            return None, "case_id は空にできません"
        if Path(case_id).name != case_id or case_id.startswith("."):
            return None, f"case_id はファイル名に使える綴りが必要です: {case_id!r}"
        if not values:
            return None, f"values は 1 入力以上が必要です: {case_id}"
        case = cls(
            case_id=case_id, input_path=Path(directory) / f"{case_id}{_CASE_SUFFIX}"
        )

        # np.savez の keyword は allow_pickle と同じ名前空間に入るため、
        # 展開する辞書の値型を Any へ落とさないと型検査が通らない。
        archived = cast("dict[str, Any]", dict(values))

        def write_archive(stream: BinaryIO) -> None:
            np.savez(stream, **archived)

        atomic_write_stream(case.input_path, write_archive)
        return case, None

    def load_values(self) -> tuple[dict[str, NDArray[np.float32]] | None, str | None]:
        """書き出した入力を読み戻す."""

        try:
            with np.load(self.input_path) as archive:
                return {name: archive[name] for name in archive.files}, None
        except Exception as error:  # noqa: BLE001 - 読み込みの失敗は理由にする
            return None, f"case の入力を読めません: {self.input_path}（{error}）"


@attrs.frozen
class ColdStartMeasurement:
    """Process 起動から初回予測までの所要と peak RSS."""

    elapsed_seconds: float
    peak_resident_kibibytes: int

    @classmethod
    def measure(
        cls,
        package: Path,
        case: BenchmarkCase,
    ) -> tuple[ColdStartMeasurement | None, str | None]:
        """子プロセスを 1 回だけ起こして cold start を測る."""

        if error := case.validate():
            return None, error
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _COLD_START_PROGRAM,
                str(package),
                str(case.input_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return None, (
                "cold start の計測に失敗しました"
                f"（exit={completed.returncode}）: {completed.stderr.strip()}"
            )
        try:
            measured = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            return None, f"cold start の計測結果を読めません: {error}"
        return (
            cls(
                elapsed_seconds=float(measured["elapsed_seconds"]),
                peak_resident_kibibytes=int(measured["peak_resident_kibibytes"]),
            ),
            None,
        )


@attrs.frozen
class CaseLatency:
    """Case 1 個ぶんの計測結果."""

    case_id: str
    statistics: LatencyStatistics


@attrs.frozen
class DeviceBenchmark:
    """1 機体 1 package ぶんの計測結果.

    ``device_label`` には機体と計測条件を人が読める形で残す。

    子プロセスの計測結果は ``sys.executable`` と環境変数に依存するため。
    """

    device_label: str
    onnxruntime_version: str
    warmup_count: int
    cases: tuple[CaseLatency, ...]
    cold_start_seconds: float
    peak_resident_kibibytes: int
    artifact_bytes: int

    @classmethod
    def measure(
        cls,
        package: Path,
        cases: Sequence[BenchmarkCase],
        *,
        device_label: str,
        warmup_count: int = 10,
        measured_count: int = 100,
    ) -> tuple[DeviceBenchmark | None, str | None]:
        """Cold start を測ってから、全 case の warm latency を測る."""

        if error := _validate_measure_arguments(
            cases, device_label, warmup_count, measured_count
        ):
            return None, error
        cold_start, error = ColdStartMeasurement.measure(package, cases[0])
        if cold_start is None:
            return None, error
        model, error = OnnxInferenceModel.load(package)
        if model is None:
            return None, error

        measured: list[CaseLatency] = []
        for case in cases:
            latency, error = _measure_case(model, case, warmup_count, measured_count)
            if latency is None:
                return None, error
            measured.append(latency)
        return (
            cls(
                device_label=device_label,
                onnxruntime_version=onnxruntime.__version__,
                warmup_count=warmup_count,
                cases=tuple(measured),
                cold_start_seconds=cold_start.elapsed_seconds,
                peak_resident_kibibytes=cold_start.peak_resident_kibibytes,
                artifact_bytes=_artifact_bytes(model),
            ),
            None,
        )

    @property
    def worst_p95_seconds(self) -> float:
        """全 case のうち最も遅い p95."""

        return max(case.statistics.p95_seconds for case in self.cases)

    def as_latency_evidence(self) -> LatencyEvidence:
        """Promote 判定へ渡す latency の証拠を組む."""

        return LatencyEvidence(
            device_label=self.device_label,
            worst_p95_seconds=self.worst_p95_seconds,
            cold_start_seconds=self.cold_start_seconds,
        )

    def validate(self) -> str | None:
        """計測結果が判定に使える形かを検証する."""

        if not self.device_label:
            return "device_label は空にできません"
        if not self.onnxruntime_version:
            return "onnxruntime_version は空にできません"
        if self.warmup_count < 0:
            return f"warmup_count は 0 以上が必要です: {self.warmup_count}"
        if not self.cases:
            return "cases は 1 件以上が必要です"
        if not math.isfinite(self.cold_start_seconds) or (
            self.cold_start_seconds <= 0.0
        ):
            return (
                f"cold_start_seconds は正の有限値が必要です: {self.cold_start_seconds}"
            )
        if self.peak_resident_kibibytes <= 0:
            return (
                "peak_resident_kibibytes は正の整数が必要です: "
                f"{self.peak_resident_kibibytes}"
            )
        if self.artifact_bytes <= 0:
            return f"artifact_bytes は正の整数が必要です: {self.artifact_bytes}"
        return None

    def save(self, path: Path) -> None:
        """計測結果を envelope 付き JSON として atomic に書き出す."""

        DEVICE_BENCHMARK_DOCUMENT.save(path, self, converter=_CONVERTER)

    @classmethod
    def load(cls, path: Path) -> tuple[DeviceBenchmark | None, str | None]:
        """計測結果を読み、構造が崩れていれば理由を返す."""

        return DEVICE_BENCHMARK_DOCUMENT.load(path, cls, converter=_CONVERTER)


def _percentile(ordered: Sequence[float], percent: int) -> float:
    """Nearest-rank の百分位を返す.

    ``ordered`` は昇順であること。
    """

    rank = math.ceil(percent / 100 * len(ordered))
    return ordered[max(rank, 1) - 1]


def _validate_measure_arguments(
    cases: Sequence[BenchmarkCase],
    device_label: str,
    warmup_count: int,
    measured_count: int,
) -> str | None:
    if not device_label:
        return "device_label は空にできません"
    if warmup_count < 0:
        return f"warmup_count は 0 以上が必要です: {warmup_count}"
    if measured_count < 1:
        return f"measured_count は 1 以上が必要です: {measured_count}"
    if not cases:
        return "cases は 1 件以上が必要です"
    identifiers = [case.case_id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        return f"case_id が重複しています: {sorted(identifiers)}"
    for case in cases:
        if error := case.validate():
            return error
    return None


def _measure_case(
    model: OnnxInferenceModel,
    case: BenchmarkCase,
    warmup_count: int,
    measured_count: int,
) -> tuple[CaseLatency | None, str | None]:
    values, error = case.load_values()
    if values is None:
        return None, error
    # warm-up の結果は捨てる。推論が失敗するなら、直後の計測 loop が同じ入力で
    # 同じ理由を返すので、ここで見ても検出できることは増えない。
    for _ in range(warmup_count):
        model.predict(values)
    durations: list[float] = []
    for _ in range(measured_count):
        started = time.perf_counter()
        outputs, error = model.predict(values)
        durations.append(time.perf_counter() - started)
        if outputs is None:
            return None, error
    statistics, error = LatencyStatistics.of(durations)
    if statistics is None:
        return None, error
    return CaseLatency(case_id=case.case_id, statistics=statistics), None


def _artifact_bytes(model: OnnxInferenceModel) -> int:
    return sum(
        (model.package_path / filename).stat().st_size
        for filename in model.manifest.payload_filenames()
    )


__all__ = [
    "DEVICE_BENCHMARK_DOCUMENT",
    "BenchmarkCase",
    "CaseLatency",
    "ColdStartMeasurement",
    "DeviceBenchmark",
    "LatencyStatistics",
]
