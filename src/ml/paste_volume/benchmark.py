"""Paste-volume production-equivalent benchmark evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import attrs
import numpy as np

from ml.export.benchmark import benchmark_environment, benchmark_runtime
from ml.infer.onnx import OnnxSession, onnxruntime_version

from .artifact import (
    BENCHMARK_SCHEMA_VERSION,
    ArtifactLineage,
    ModelFormat,
    artifact_sha256 as _sha256_file,
    atomic_json_new as _atomic_json_new,
    canonical_json as _canonical_json,
    is_nonnegative_finite as _is_nonnegative_finite,
    is_positive_finite as _is_positive_finite,
    is_prefixed_sha256 as _is_prefixed_sha256,
    lineage_from_mapping as _lineage_from_mapping,
    read_json as _read_json,
    required_mapping as _required_mapping,
    validate_canonical_preprocess_schema as _validate_canonical_preprocess_schema,
)
from .onnx import (
    read_onnx_artifact_lineage,
    read_onnx_export_metadata,
    read_onnx_preprocess_schema,
    resolve_evaluation_dataset,
    validate_candidate_artifact,
)

PI_P95_LATENCY_MS_MAX = 1000.0
BENCHMARK_WARMUP_ITERATIONS = 10
BENCHMARK_MEASURED_ITERATIONS = 100
type BenchmarkCategory = Literal["small", "medium", "large", "portrait", "landscape"]
_BENCHMARK_CATEGORIES: tuple[BenchmarkCategory, ...] = (
    "small",
    "medium",
    "large",
    "portrait",
    "landscape",
)


@attrs.frozen
class BenchmarkCategoryResult:
    """1つの代表categoryを独立に10/100回測定した証跡."""

    category: BenchmarkCategory
    sample_id: str
    image_height: int
    image_width: int
    warmup_iterations: int
    measured_iterations: int
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float

    @property
    def gate_passed(self) -> bool:
        return (
            self.category in _BENCHMARK_CATEGORIES
            and bool(self.sample_id)
            and self.image_height > 0
            and self.image_width > 0
            and self.warmup_iterations == BENCHMARK_WARMUP_ITERATIONS
            and self.measured_iterations == BENCHMARK_MEASURED_ITERATIONS
            and all(
                _is_nonnegative_finite(value)
                for value in (
                    self.p50_latency_ms,
                    self.p95_latency_ms,
                    self.p99_latency_ms,
                )
            )
            and self.p95_latency_ms <= PI_P95_LATENCY_MS_MAX
        )

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class ModelBenchmarkResult:
    """Production同等preprocessを含むbatch-1 CPU benchmark."""

    schema_version: int
    candidate_id: str
    model_artifact_sha256: str
    source_run_id: str
    source_checkpoint_sha256: str
    source_checkpoint_role: Literal["best"]
    training_dataset_fingerprint: str
    training_split_fingerprint: str
    training_protocol_fingerprint: str
    dataset_fingerprint: str
    split_fingerprint: str
    benchmark_sample_ids: tuple[str, ...]
    parent_run_id: str | None
    parent_checkpoint_id: str | None
    platform_model: str
    is_raspberry_pi_5: bool
    cold_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    peak_rss_bytes: int
    artifact_size_bytes: int
    sample_count: int
    warmup_iterations: int
    measured_iterations: int
    category_results: tuple[BenchmarkCategoryResult, ...]
    cold_start_clock: str
    cold_start_origin: str
    os_id: str
    os_release: str
    python_version: str
    onnxruntime_version: str
    cpu_governor: str
    platform_machine: str
    power_condition: str
    cooling_condition: str

    @property
    def gate_passed(self) -> bool:
        return (
            self.is_raspberry_pi_5
            and self.p95_latency_ms <= PI_P95_LATENCY_MS_MAX
            and type(self.warmup_iterations) is int
            and self.warmup_iterations == BENCHMARK_WARMUP_ITERATIONS
            and type(self.measured_iterations) is int
            and self.measured_iterations == BENCHMARK_MEASURED_ITERATIONS
            and self.sample_count == len(_BENCHMARK_CATEGORIES)
            and tuple(result.category for result in self.category_results)
            == _BENCHMARK_CATEGORIES
            and len({result.sample_id for result in self.category_results})
            == len(_BENCHMARK_CATEGORIES)
            and all(result.gate_passed for result in self.category_results)
            and self.p95_latency_ms
            == max(result.p95_latency_ms for result in self.category_results)
            and self.platform_machine.lower() == "aarch64"
            and all(
                value.strip()
                for value in (
                    self.cold_start_clock,
                    self.cold_start_origin,
                    self.os_id,
                    self.os_release,
                    self.python_version,
                    self.onnxruntime_version,
                    self.cpu_governor,
                    self.power_condition,
                    self.cooling_condition,
                )
            )
        )

    def to_dict(self) -> dict[str, object]:
        value = attrs.asdict(self)
        value["benchmark_sample_ids"] = list(self.benchmark_sample_ids)
        value["category_results"] = [
            result.to_dict() for result in self.category_results
        ]
        return value


@attrs.frozen
class BenchmarkSample:
    """Benchmarkへ渡す代表的なlossless RGB pair."""

    sample_id: str
    category: BenchmarkCategory
    pre_rgb: np.ndarray[Any, np.dtype[np.uint8]] = attrs.field(eq=False)
    post_rgb: np.ndarray[Any, np.dtype[np.uint8]] = attrs.field(eq=False)
    pixel_per_mm: float


def benchmark_model_candidate(
    model_path: Path,
    *,
    model_format: ModelFormat,
    preprocess_schema: Mapping[str, Any],
    lineage: ArtifactLineage,
    evaluation_dataset_fingerprint: str,
    evaluation_split_fingerprint: str,
    samples: Sequence[BenchmarkSample],
    power_condition: str,
    cooling_condition: str,
    warmup_iterations: int = BENCHMARK_WARMUP_ITERATIONS,
    measured_iterations: int = BENCHMARK_MEASURED_ITERATIONS,
) -> ModelBenchmarkResult:
    """同じPython前処理とCPU ORTでcold/warm latencyを実測する.

    promotion可能なreportはRaspberry Pi 5上で実行した場合だけ生成される。
    """

    if not power_condition.strip() or not cooling_condition.strip():
        raise ValueError("benchmarkのpower/cooling condition申告が必要です")
    if not evaluation_dataset_fingerprint or not evaluation_split_fingerprint:
        raise ValueError("benchmark evaluation dataset/split fingerprintが必要です")
    sample_ids = tuple(sample.sample_id for sample in samples)
    if any(not sample_id for sample_id in sample_ids):
        raise ValueError("benchmark sample_idを空にできません")
    if len(set(sample_ids)) != len(sample_ids):
        raise ValueError("benchmark sample_idが重複しています")
    by_category = {sample.category: sample for sample in samples}
    if (
        len(by_category) != len(samples)
        or tuple(
            category for category in _BENCHMARK_CATEGORIES if category in by_category
        )
        != _BENCHMARK_CATEGORIES
        or len(samples) != len(_BENCHMARK_CATEGORIES)
    ):
        raise ValueError(
            "benchmarkにはsmall/medium/large/portrait/landscape各1sampleが必要です"
        )
    ordered_samples = tuple(by_category[category] for category in _BENCHMARK_CATEGORIES)
    sample_ids = tuple(sample.sample_id for sample in ordered_samples)
    if (
        type(warmup_iterations) is not int
        or warmup_iterations != BENCHMARK_WARMUP_ITERATIONS
    ):
        raise ValueError(
            f"production benchmark warm-upは{BENCHMARK_WARMUP_ITERATIONS}回が必要です"
        )
    if (
        type(measured_iterations) is not int
        or measured_iterations != BENCHMARK_MEASURED_ITERATIONS
    ):
        raise ValueError(
            f"production benchmark測定は{BENCHMARK_MEASURED_ITERATIONS}回が必要です"
        )
    _validate_canonical_preprocess_schema(preprocess_schema)
    path = Path(model_path).expanduser().resolve(strict=True)
    validate_candidate_artifact(path, model_format)
    artifact_hash = _sha256_file(path)
    candidate_id = _candidate_id(model_format, artifact_hash)
    metadata = read_onnx_export_metadata(path)
    embedded_lineage = _lineage_from_mapping(_required_mapping(metadata, "lineage"))
    if embedded_lineage != lineage:
        raise ValueError("benchmark modelのembedded training lineageが不一致です")
    embedded_preprocess = _required_mapping(metadata, "preprocess_schema")
    if _canonical_json(embedded_preprocess) != _canonical_json(preprocess_schema):
        raise ValueError("benchmark modelのpreprocess schemaが不一致です")

    timing = benchmark_runtime(
        lambda: OnnxSession.load_cpu(path),
        tuple((sample.category, sample) for sample in ordered_samples),
        lambda session, sample: _run_benchmark_sample(
            session, sample, preprocess_schema
        ),
        warmup_iterations=warmup_iterations,
        measured_iterations=measured_iterations,
    )
    category_results = tuple(
        BenchmarkCategoryResult(
            category=sample.category,
            sample_id=sample.sample_id,
            image_height=int(sample.pre_rgb.shape[0]),
            image_width=int(sample.pre_rgb.shape[1]),
            warmup_iterations=warmup_iterations,
            measured_iterations=measured_iterations,
            p50_latency_ms=latency.p50_latency_ms,
            p95_latency_ms=latency.p95_latency_ms,
            p99_latency_ms=latency.p99_latency_ms,
        )
        for sample, latency in zip(ordered_samples, timing.cases, strict=True)
    )
    environment = benchmark_environment()
    model_name = environment.platform_model
    return ModelBenchmarkResult(
        schema_version=BENCHMARK_SCHEMA_VERSION,
        candidate_id=candidate_id,
        model_artifact_sha256=artifact_hash,
        source_run_id=lineage.source_run_id,
        source_checkpoint_sha256=lineage.source_checkpoint_sha256,
        source_checkpoint_role=lineage.source_checkpoint_role,
        training_dataset_fingerprint=lineage.dataset_fingerprint,
        training_split_fingerprint=lineage.split_fingerprint,
        training_protocol_fingerprint=lineage.training_protocol_fingerprint,
        dataset_fingerprint=evaluation_dataset_fingerprint,
        split_fingerprint=evaluation_split_fingerprint,
        benchmark_sample_ids=sample_ids,
        parent_run_id=lineage.parent_run_id,
        parent_checkpoint_id=lineage.parent_checkpoint_id,
        platform_model=model_name,
        is_raspberry_pi_5="raspberry pi 5" in model_name.lower(),
        cold_latency_ms=timing.cold_latency_ms,
        p50_latency_ms=timing.p50_latency_ms,
        p95_latency_ms=timing.p95_latency_ms,
        p99_latency_ms=timing.p99_latency_ms,
        peak_rss_bytes=environment.peak_rss_bytes,
        artifact_size_bytes=path.stat().st_size,
        sample_count=len(ordered_samples),
        warmup_iterations=warmup_iterations,
        measured_iterations=measured_iterations,
        category_results=category_results,
        cold_start_clock=timing.cold_start_clock,
        cold_start_origin=timing.cold_start_origin,
        os_id=environment.os_id,
        os_release=environment.os_release,
        python_version=environment.python_version,
        onnxruntime_version=onnxruntime_version(),
        cpu_governor=environment.cpu_governor,
        platform_machine=environment.platform_machine,
        power_condition=power_condition.strip(),
        cooling_condition=cooling_condition.strip(),
    )


def benchmark_model_candidate_from_dataset(
    model_path: Path,
    *,
    model_format: ModelFormat,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    power_condition: str,
    cooling_condition: str,
    warmup_iterations: int = BENCHMARK_WARMUP_ITERATIONS,
    measured_iterations: int = BENCHMARK_MEASURED_ITERATIONS,
) -> ModelBenchmarkResult:
    """Persisted validationから小/中/大/縦長/横長の代表sampleを選びPi計測する."""

    lineage = read_onnx_artifact_lineage(model_path)
    composite, samples, split = resolve_evaluation_dataset(
        dataset_paths,
        split_manifest,
        lineage=lineage,
        require_training_lineage_match=True,
    )
    by_id = {sample.sample_id: sample for sample in samples}
    validation_samples = [by_id[sample_id] for sample_id in split.validation_sample_ids]
    if not validation_samples:
        raise ValueError("validation splitにbenchmark sampleがありません")
    representatives = _representative_samples(validation_samples)
    from torchvision.io import ImageReadMode, decode_image

    benchmark_samples: list[BenchmarkSample] = []
    for category, sample in representatives:
        pre = (
            decode_image(str(sample.pre_path), mode=ImageReadMode.RGB)
            .movedim(0, -1)
            .contiguous()
            .numpy()
        )
        post = (
            decode_image(str(sample.post_path), mode=ImageReadMode.RGB)
            .movedim(0, -1)
            .contiguous()
            .numpy()
        )
        benchmark_samples.append(
            BenchmarkSample(
                sample_id=sample.sample_id,
                category=category,
                pre_rgb=pre,
                post_rgb=post,
                pixel_per_mm=sample.pixel_per_mm,
            )
        )
    return benchmark_model_candidate(
        model_path,
        model_format=model_format,
        preprocess_schema=read_onnx_preprocess_schema(model_path),
        lineage=lineage,
        evaluation_dataset_fingerprint=composite.composite_fingerprint,
        evaluation_split_fingerprint=split.split_fingerprint,
        samples=benchmark_samples,
        power_condition=power_condition,
        cooling_condition=cooling_condition,
        warmup_iterations=warmup_iterations,
        measured_iterations=measured_iterations,
    )


def _representative_samples(
    samples: Sequence[Any],
) -> tuple[tuple[BenchmarkCategory, Any], ...]:
    if len({sample.sample_id for sample in samples}) < len(_BENCHMARK_CATEGORIES):
        raise ValueError("benchmarkの5 categoryには5件以上の異なるsampleが必要です")
    portrait_candidates = [sample for sample in samples if sample.aspect_ratio < 1.0]
    landscape_candidates = [sample for sample in samples if sample.aspect_ratio > 1.0]
    if not portrait_candidates or not landscape_candidates:
        raise ValueError("benchmarkにはportraitとlandscape sampleが必要です")
    portrait = min(
        portrait_candidates,
        key=lambda sample: (sample.aspect_ratio, sample.sample_id),
    )
    landscape = max(
        landscape_candidates,
        key=lambda sample: (sample.aspect_ratio, sample.sample_id),
    )
    reserved = {portrait.sample_id, landscape.sample_id}
    area_candidates = sorted(
        (sample for sample in samples if sample.sample_id not in reserved),
        key=lambda sample: (sample.image_area_pixels, sample.sample_id),
    )
    distinct_areas = sorted({sample.image_area_pixels for sample in area_candidates})
    if len(distinct_areas) < 3:
        raise ValueError(
            "benchmarkのsmall/medium/largeには3段階の異なる画像面積が必要です"
        )
    small_area = distinct_areas[0]
    medium_area = distinct_areas[len(distinct_areas) // 2]
    large_area = distinct_areas[-1]

    def first_at_area(area: int) -> Any:
        return next(
            sample for sample in area_candidates if sample.image_area_pixels == area
        )

    return (
        ("small", first_at_area(small_area)),
        ("medium", first_at_area(medium_area)),
        ("large", first_at_area(large_area)),
        ("portrait", portrait),
        ("landscape", landscape),
    )


def _run_benchmark_sample(
    session: Any,
    sample: BenchmarkSample,
    preprocess_schema: Mapping[str, Any],
) -> None:
    from .data import ImageConstraints, preprocess_rgb_pair

    _validate_rgb_array(sample.pre_rgb, "pre_rgb")
    _validate_rgb_array(sample.post_rgb, "post_rgb")
    if sample.pre_rgb.shape != sample.post_rgb.shape:
        raise ValueError("benchmark pre/post shapeが一致しません")
    if not _is_positive_finite(sample.pixel_per_mm):
        raise ValueError("benchmark pixel_per_mmは正の有限値が必要です")
    raw = _required_mapping(preprocess_schema, "image_constraints")
    processed = preprocess_rgb_pair(
        sample.pre_rgb,
        sample.post_rgb,
        sample.pixel_per_mm,
        constraints=ImageConstraints(
            min_size=int(raw["min_size"]),
            max_size=int(raw["max_size"]),
            max_pixels=int(raw["max_pixels"]),
            stride=int(raw["stride"]),
            normalization_epsilon=float(raw["normalization_epsilon"]),
        ),
    )
    result = session.run(
        ["mean_volume_ul", "log_variance_volume_ul2"],
        {
            "image_6ch": processed.image_6ch.detach().cpu().float().numpy()[None, ...],
            "valid_pixel_mask": processed.valid_pixel_mask.detach()
            .cpu()
            .numpy()[None, ...]
            .astype(np.bool_),
            "pixel_per_mm": np.asarray([[processed.pixel_per_mm]], dtype=np.float32),
        },
    )
    if len(result) != 2:
        raise RuntimeError("benchmark model output数が不正です")
    mean = float(np.asarray(result[0]).item())
    log_variance = float(np.asarray(result[1]).item())
    if not math.isfinite(mean) or mean <= 0 or not math.isfinite(log_variance):
        raise RuntimeError("benchmark modelが不正な出力を返しました")


def save_model_benchmark_result(path: Path, benchmark: ModelBenchmarkResult) -> Path:
    output = Path(path).expanduser().resolve()
    _atomic_json_new(output, benchmark.to_dict())
    return output


def load_model_benchmark_result(path: Path) -> ModelBenchmarkResult:
    raw = dict(_read_json(Path(path).expanduser().resolve(strict=True)))
    expected = {field.name for field in attrs.fields(ModelBenchmarkResult)}
    if set(raw) != expected:
        raise ValueError("benchmark reportのkey集合が不正です")
    sample_ids = raw.get("benchmark_sample_ids")
    if not isinstance(sample_ids, list) or not all(
        isinstance(item, str) and item for item in sample_ids
    ):
        raise ValueError("benchmark_sample_idsは空でない文字列arrayが必要です")
    raw["benchmark_sample_ids"] = tuple(sample_ids)
    category_values = raw.get("category_results")
    if not isinstance(category_values, list):
        raise ValueError("benchmark category_resultsはarrayが必要です")
    category_expected = {field.name for field in attrs.fields(BenchmarkCategoryResult)}
    category_results: list[BenchmarkCategoryResult] = []
    for index, value in enumerate(category_values):
        if not isinstance(value, dict) or set(value) != category_expected:
            raise ValueError(f"benchmark category_results[{index}]のkey集合が不正です")
        try:
            category_results.append(BenchmarkCategoryResult(**value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"benchmark category_results[{index}]が不正です: {exc}"
            ) from exc
    raw["category_results"] = tuple(category_results)
    try:
        result = ModelBenchmarkResult(**raw)
    except TypeError as exc:
        raise ValueError(f"benchmark report schemaが不正です: {exc}") from exc
    if result.schema_version != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("未知のbenchmark report schemaです")
    validate_benchmark_result(result)
    return result


def _candidate_id(model_format: ModelFormat, artifact_hash: str) -> str:
    return f"{model_format}:{artifact_hash[:16]}"


def validate_benchmark_result(benchmark: ModelBenchmarkResult) -> None:
    if (
        type(benchmark.warmup_iterations) is not int
        or benchmark.warmup_iterations != BENCHMARK_WARMUP_ITERATIONS
    ):
        raise ValueError(
            f"production benchmark warm-upは{BENCHMARK_WARMUP_ITERATIONS}回が必要です"
        )
    if (
        type(benchmark.measured_iterations) is not int
        or benchmark.measured_iterations != BENCHMARK_MEASURED_ITERATIONS
    ):
        raise ValueError(
            f"production benchmark測定は{BENCHMARK_MEASURED_ITERATIONS}回が必要です"
        )
    if benchmark.schema_version != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("未知のbenchmark report schemaです")
    if tuple(result.category for result in benchmark.category_results) != (
        _BENCHMARK_CATEGORIES
    ):
        raise ValueError(
            "benchmark categoryはsmall/medium/large/portrait/landscapeの固定順が必要です"
        )
    if benchmark.sample_count != len(_BENCHMARK_CATEGORIES):
        raise ValueError("benchmark sample_countは5が必要です")
    if benchmark.benchmark_sample_ids != tuple(
        result.sample_id for result in benchmark.category_results
    ):
        raise ValueError("benchmark sample IDとcategory証跡が不一致です")
    if len(set(benchmark.benchmark_sample_ids)) != len(_BENCHMARK_CATEGORIES):
        raise ValueError("benchmark category間でsampleを再利用できません")
    for result in benchmark.category_results:
        if not result.gate_passed:
            raise ValueError(
                f"benchmark category証跡が不正またはp95 gate失敗です: {result.category}"
            )
    numeric_values = (
        benchmark.cold_latency_ms,
        benchmark.p50_latency_ms,
        benchmark.p95_latency_ms,
        benchmark.p99_latency_ms,
    )
    if not all(_is_nonnegative_finite(value) for value in numeric_values):
        raise ValueError("benchmark latencyは非負の有限値が必要です")
    if benchmark.p95_latency_ms != max(
        result.p95_latency_ms for result in benchmark.category_results
    ):
        raise ValueError("benchmark aggregate p95がcategory最悪値と不一致です")
    if benchmark.p99_latency_ms != max(
        result.p99_latency_ms for result in benchmark.category_results
    ):
        raise ValueError("benchmark aggregate p99がcategory最悪値と不一致です")
    if type(benchmark.peak_rss_bytes) is not int or benchmark.peak_rss_bytes < 0:
        raise ValueError("benchmark peak_rss_bytesが不正です")
    if (
        type(benchmark.artifact_size_bytes) is not int
        or benchmark.artifact_size_bytes < 1
    ):
        raise ValueError("benchmark artifact_size_bytesが不正です")
    if benchmark.cold_start_clock != "CLOCK_MONOTONIC":
        raise ValueError("benchmark cold start clockはCLOCK_MONOTONICが必要です")
    for name in (
        "cold_start_origin",
        "os_id",
        "os_release",
        "python_version",
        "onnxruntime_version",
        "cpu_governor",
        "platform_machine",
        "power_condition",
        "cooling_condition",
        "platform_model",
    ):
        value = getattr(benchmark, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"benchmark {name}を空にできません")
    for name in (
        "source_checkpoint_sha256",
        "training_dataset_fingerprint",
        "training_split_fingerprint",
        "training_protocol_fingerprint",
        "dataset_fingerprint",
        "split_fingerprint",
    ):
        if not _is_prefixed_sha256(getattr(benchmark, name)):
            raise ValueError(f"benchmark {name}が不正です")
    if benchmark.source_checkpoint_role != "best":
        raise ValueError("benchmark source checkpoint roleはbestが必要です")
    if (benchmark.parent_run_id is None) != (benchmark.parent_checkpoint_id is None):
        raise ValueError("benchmark fine-tune parent lineageが片方だけです")


def _validate_rgb_array(value: object, name: str) -> None:
    if not isinstance(value, np.ndarray):
        raise TypeError(f"{name}はnumpy.ndarrayが必要です")
    if value.dtype != np.uint8 or value.ndim != 3 or value.shape[2] != 3:
        raise ValueError(f"{name}はuint8 HWC RGB画像が必要です")
