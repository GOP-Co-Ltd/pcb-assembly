"""Hydra entrypoint for reproducible PyTorch evaluation runs."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

import torch
from torch import Tensor

from .compile_parity import torch_compile_graph_break_count
from .data import (
    DatasetValidationReport,
    sample_index_fingerprint,
    save_composite_manifest,
    save_sample_index,
    save_split_manifest,
)
from .dependencies import dependency_versions, git_provenance, runtime_identity
from .experiment import ExperimentLogger, MLflowExperimentLogger, write_json_artifact
from .formal_artifact import publish_formal_artifact
from .model import (
    PasteVolumeModelConfig,
    PasteVolumeResNet,
    model_gmac,
    model_parameter_count,
)
from .reporting import DiagnosticReport, Prediction, build_diagnostic_report
from .train import (
    DataConfig,
    LoggerConfig,
    PreparedTrainingData,
    TrainConfig,
    prepare_training_data,
    preprocess_schema,
    sanitize_persisted_text,
    sanitize_persisted_value,
    summarize_persisted_git_diff,
    train_config_from_mapping,
)
from .training import (
    CheckpointConfig,
    FormalTrainingWeights,
    RegressionMetrics,
    TrainerConfig,
    TrainingData,
    evaluate_batches,
    load_formal_training_weights,
    load_model_weights,
)

EVALUATION_REPORT_KIND = "pcbasm-paste-volume-evaluation-report"
EVALUATION_REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EvaluateConfig:
    data: DataConfig
    weights: Path
    output_directory: Path
    split: Literal["validation", "test"] = "validation"
    allow_frozen_test: bool = False
    allow_external_split: bool = False
    device: str = "auto"
    compile_enabled: bool = True
    compile_backend: str = "inductor"
    compile_mode: str = "default"
    repository_root: Path = Path(".")
    hydra_resolved_yaml: str | None = field(default=None, repr=False)
    hydra_overrides: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.data.split_manifest is None:
            raise ValueError("evaluation requires a frozen data.split_manifest")
        if self.split not in ("validation", "test"):
            raise ValueError("split must be validation or test")
        if type(self.allow_frozen_test) is not bool:
            raise TypeError("allow_frozen_test must be a boolean")
        if type(self.allow_external_split) is not bool:
            raise TypeError("allow_external_split must be a boolean")
        if (self.split == "test") != self.allow_frozen_test:
            raise ValueError(
                "diagnostic test evaluation requires split=test and "
                "allow_frozen_test=true together"
            )
        if self.device not in ("auto", "cpu", "cuda"):
            raise ValueError("device must be auto, cpu, or cuda")
        if type(self.compile_enabled) is not bool:
            raise TypeError("compile_enabled must be a boolean")
        if not self.compile_backend.strip() or not self.compile_mode.strip():
            raise ValueError("compile backend and mode must be non-empty")

    @property
    def diagnostic_only(self) -> bool:
        return self.split == "test"

    @property
    def release_evidence(self) -> bool:
        return self.split == "validation"

    def to_dict(self) -> dict[str, object]:
        return {
            "data": self.data.to_dict(),
            "weights": str(self.weights),
            "output_directory": str(self.output_directory),
            "split": self.split,
            "allow_frozen_test": self.allow_frozen_test,
            "allow_external_split": self.allow_external_split,
            "device": self.device,
            "compile_enabled": self.compile_enabled,
            "compile_backend": self.compile_backend,
            "compile_mode": self.compile_mode,
            "repository_root": str(self.repository_root),
        }


@dataclass(frozen=True)
class EvaluationResult:
    run_id: str
    metrics: RegressionMetrics
    report_path: Path
    dataset_fingerprint: str
    split_fingerprint: str
    weights_sha256: str
    split: Literal["validation", "test"]
    training_dataset_fingerprint: str
    training_split_fingerprint: str
    training_protocol_fingerprint: str
    external_evaluation_split: bool
    diagnostic_only: bool
    release_evidence: bool
    predictions: tuple[Prediction, ...]
    diagnostics: DiagnosticReport
    diagnostic_report_path: Path
    diagnostic_report_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_fingerprint(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _resolve_device(value: str) -> torch.device:
    if value == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device = torch.device(value)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available")
    return device


def _synchronize(device: torch.device) -> None:
    if device.type == "cuda":
        torch.cuda.synchronize(device)


def _validate_outputs(outputs: tuple[Tensor, Tensor]) -> None:
    mean, log_variance = outputs
    if mean.shape != log_variance.shape or mean.ndim != 2 or mean.shape[1] != 1:
        raise ValueError("model smoke outputs must both have shape [B, 1]")
    if (
        not torch.all(torch.isfinite(mean)).item()
        or not torch.all(torch.isfinite(log_variance)).item()
    ):
        raise ValueError("model smoke outputs must be finite")


def _prepare_forward(
    model: PasteVolumeResNet,
    *,
    source: TrainingData,
    split: Literal["validation", "test"],
    device: torch.device,
    enabled: bool,
    backend: str,
    mode: str,
) -> tuple[PasteVolumeResNet, dict[str, object], int]:
    batch_plan = source.evaluation_batch_plan(split)
    if not batch_plan:
        raise ValueError(f"evaluation split is empty: {split}")
    smoke = source.evaluation_batch(batch_plan[0], split=split).to(device)
    inputs = (smoke.image_6ch, smoke.valid_pixel_mask, smoke.pixel_per_mm)

    _synchronize(device)
    eager_started = time.perf_counter()
    with torch.inference_mode():
        eager_outputs = model(*inputs)
    _synchronize(device)
    eager_seconds = max(time.perf_counter() - eager_started, 0.0)
    _validate_outputs(eager_outputs)

    if not enabled:
        return (
            model,
            {
                "enabled": False,
                "backend": backend,
                "mode": mode,
                "fullgraph": False,
                "dynamic": None,
                "graph_break_count": 0,
                "eager_smoke_seconds": eager_seconds,
                "compile_and_first_forward_seconds": None,
                "compiled_replay_seconds": None,
                "smoke_input_shape": list(smoke.image_6ch.shape),
            },
            torch_compile_graph_break_count(),
        )

    graph_breaks_before = torch_compile_graph_break_count()
    compile_started = time.perf_counter()
    forward = cast(
        PasteVolumeResNet,
        torch.compile(
            model,
            backend=backend,
            mode=mode,
            fullgraph=False,
            dynamic=None,
        ),
    )
    with torch.inference_mode():
        compiled_outputs = forward(*inputs)
    _synchronize(device)
    first_seconds = max(time.perf_counter() - compile_started, 0.0)
    _validate_outputs(compiled_outputs)
    torch.testing.assert_close(compiled_outputs[0], eager_outputs[0])
    torch.testing.assert_close(compiled_outputs[1], eager_outputs[1])

    replay_started = time.perf_counter()
    with torch.inference_mode():
        replay_outputs = forward(*inputs)
    _synchronize(device)
    replay_seconds = max(time.perf_counter() - replay_started, 0.0)
    _validate_outputs(replay_outputs)
    graph_break_count = max(0, torch_compile_graph_break_count() - graph_breaks_before)
    if graph_break_count != 0:
        raise RuntimeError(
            f"torch.compile evaluation preflight observed {graph_break_count} graph breaks"
        )
    return (
        forward,
        {
            "enabled": True,
            "backend": backend,
            "mode": mode,
            "fullgraph": False,
            "dynamic": None,
            "graph_break_count": graph_break_count,
            "eager_smoke_seconds": eager_seconds,
            "compile_and_first_forward_seconds": first_seconds,
            "compiled_replay_seconds": replay_seconds,
            "smoke_input_shape": list(smoke.image_6ch.shape),
        },
        graph_breaks_before,
    )


def _flatten_parameters(
    value: Mapping[str, object],
) -> dict[str, str | int | float | bool]:
    parameters: dict[str, str | int | float | bool] = {}

    def visit(prefix: str, item: object) -> None:
        if isinstance(item, Mapping):
            for key in sorted(item):
                visit(f"{prefix}.{key}" if prefix else str(key), item[key])
            return
        if isinstance(item, (list, tuple)):
            parameters[prefix] = json.dumps(item, ensure_ascii=True, sort_keys=True)
            return
        if item is None:
            parameters[prefix] = "null"
            return
        if isinstance(item, (str, int, float, bool)):
            parameters[prefix] = item
            return
        parameters[prefix] = str(item)

    visit("", value)
    return cast(
        dict[str, str | int | float | bool], sanitize_persisted_value(parameters)
    )


def _prepare_output_directory(path: Path) -> Path:
    output = path.expanduser().resolve()
    if output.exists():
        if not output.is_dir():
            raise ValueError(
                f"evaluation output_directory is not a directory: {output}"
            )
        if any(output.iterdir()):
            raise FileExistsError(
                f"evaluation output_directory must be empty: {output}"
            )
    else:
        output.mkdir(parents=True)
    return output


def _write_provenance_artifacts(
    *,
    config: EvaluateConfig,
    output: Path,
    prepared: PreparedTrainingData,
    model: PasteVolumeResNet,
    model_config: PasteVolumeModelConfig,
    weights_sha256: str,
    compile_preflight: Mapping[str, object],
    formal_weights: FormalTrainingWeights | None,
) -> tuple[tuple[tuple[Path, str], ...], dict[str, str | int | float | bool], str]:
    composite_path = output / "composite.json"
    save_composite_manifest(prepared.composite, composite_path)
    sample_index_path = output / "sample-index.json"
    save_sample_index(prepared.samples, sample_index_path)
    split_path = output / "split.json"
    save_split_manifest(prepared.split, split_path)
    sample_fingerprint = sample_index_fingerprint(prepared.samples)
    dataset_validation = DatasetValidationReport(
        composite_fingerprint=prepared.composite.composite_fingerprint,
        content_fingerprint=prepared.composite.content_fingerprint,
        source_count=len(prepared.composite.sources),
        session_count=len(prepared.composite.sessions),
        sample_count=len(prepared.samples),
    ).to_dict()
    dataset_validation_path = write_json_artifact(
        output / "dataset-validation.json", dataset_validation
    )
    dependency_payload = dependency_versions()
    dependency_path = write_json_artifact(
        output / "dependencies.json", dependency_payload
    )
    config_payload = cast(dict[str, object], sanitize_persisted_value(config.to_dict()))
    config_fingerprint = _canonical_fingerprint(config_payload)
    frozen_config_path = write_json_artifact(
        output / "config.json",
        {**config_payload, "config_fingerprint": config_fingerprint},
    )
    model_summary = {
        "model_class": type(model).__name__,
        "model_config": model_config.to_dict(),
        "parameter_count": model_parameter_count(model),
        "gmac_at_512x512": model_gmac(model_config),
        "weights_sha256": weights_sha256,
    }
    model_summary_path = write_json_artifact(
        output / "model-summary.json", model_summary
    )
    compile_path = write_json_artifact(
        output / "compile-preflight.json", compile_preflight
    )
    parameters = _flatten_parameters(
        {
            "config": config_payload,
            "dependencies": dependency_payload,
            "model": model_summary,
            "preprocess": preprocess_schema(config.data.constraints),
            "formal_training_weights_verified": formal_weights is not None,
        }
    )
    parameters_path = write_json_artifact(
        output / "parameters.json", cast(Mapping[str, object], parameters)
    )

    provenance = git_provenance(config.repository_root)
    persisted_diff = summarize_persisted_git_diff(provenance.diff)
    git_diff_path = output / "git.diff"
    git_diff_path.write_text(persisted_diff + "\n", encoding="utf-8")
    git_payload = provenance.to_dict()
    git_payload["diff"] = persisted_diff
    git_payload["untracked_content"] = "[omitted at persistence boundary]"
    git_path = write_json_artifact(
        output / "git.json",
        cast(Mapping[str, object], sanitize_persisted_value(git_payload)),
    )

    paths: list[tuple[Path, str]] = [
        (composite_path, "provenance"),
        (sample_index_path, "provenance"),
        (split_path, "provenance"),
        (dataset_validation_path, "provenance"),
        (dependency_path, "provenance"),
        (frozen_config_path, "provenance"),
        (model_summary_path, "provenance"),
        (compile_path, "provenance"),
        (parameters_path, "provenance"),
        (git_diff_path, "provenance"),
        (git_path, "provenance"),
    ]
    if config.hydra_resolved_yaml is not None:
        resolved_path = output / "resolved-config.yaml"
        resolved_path.write_text(
            sanitize_persisted_text(config.hydra_resolved_yaml), encoding="utf-8"
        )
        overrides_path = write_json_artifact(
            output / "hydra-overrides.json",
            {
                "overrides": [
                    sanitize_persisted_text(item) for item in config.hydra_overrides
                ]
            },
        )
        paths.extend(((resolved_path, "provenance"), (overrides_path, "provenance")))
    return tuple(paths), parameters, sample_fingerprint


def _failure_close(logger: ExperimentLogger, error: Exception) -> None:
    try:
        logger.set_tags({"failure_reason": type(error).__name__})
    except Exception:
        pass
    try:
        logger.end(status="FAILED")
    except Exception:
        pass


def evaluate(
    config: EvaluateConfig,
    logger: ExperimentLogger,
    *,
    formal_tracking_uri: str | None = None,
) -> EvaluationResult:
    """Evaluate one persisted split without performing candidate selection.

    ``split=test`` is intentionally diagnostic-only. Release frozen-test evidence
    is produced exclusively by the one-shot candidate pipeline, not this generic
    Hydra evaluator.
    """

    run_id = logger.start(
        run_kind="evaluate",
        tags={
            "evaluation_split": config.split,
            "diagnostic_only": str(config.diagnostic_only).lower(),
            "release_evidence": str(config.release_evidence).lower(),
        },
    )
    try:
        formal_weights = (
            load_formal_training_weights(
                config.weights, tracking_uri=formal_tracking_uri
            )
            if formal_tracking_uri is not None
            else None
        )
        payload = load_model_weights(config.weights)
        model_raw = dict(cast(Mapping[str, object], payload["model_config"]))
        for key in ("stem_channels", "stage_channels", "blocks_per_stage"):
            model_raw[key] = tuple(cast(list[int] | tuple[int, ...], model_raw[key]))
        model_config = PasteVolumeModelConfig(**cast(Any, model_raw))
        if payload["preprocess_schema"] != preprocess_schema(config.data.constraints):
            raise ValueError(
                "weight preprocess schema does not match evaluation data constraints"
            )
        preparation_config = TrainConfig(
            data=config.data,
            checkpoint=CheckpointConfig(directory=config.output_directory),
            model=model_config,
            trainer=TrainerConfig(
                device=config.device,
                compile_enabled=config.compile_enabled,
                compile_backend=config.compile_backend,
                compile_mode=config.compile_mode,
                max_epochs=1,
            ),
            repository_root=config.repository_root,
        )
        prepared = prepare_training_data(preparation_config)
        training_dataset_fingerprint = str(payload["dataset_fingerprint"])
        training_split_fingerprint = str(payload["split_fingerprint"])
        training_protocol_fingerprint = str(payload["training_protocol_fingerprint"])
        lineage_matches = (
            training_dataset_fingerprint == prepared.composite.composite_fingerprint
            and training_split_fingerprint == prepared.split.split_fingerprint
        )
        if not lineage_matches and not config.allow_external_split:
            raise ValueError(
                "weight training lineage does not match evaluation split; "
                "set allow_external_split=true for an explicitly persisted split"
            )
        if lineage_matches and config.allow_external_split:
            raise ValueError(
                "allow_external_split=true requires a different persisted "
                "evaluation dataset or split"
            )

        output = _prepare_output_directory(config.output_directory)
        device = _resolve_device(config.device)
        model = PasteVolumeResNet(model_config).to(device)
        model.load_state_dict(
            cast(dict[str, Tensor], payload["state_dict"]), strict=True
        )
        model.eval()
        forward, compile_preflight, graph_break_baseline = _prepare_forward(
            model,
            source=prepared.source,
            split=config.split,
            device=device,
            enabled=config.compile_enabled,
            backend=config.compile_backend,
            mode=config.compile_mode,
        )
        weights_sha256 = (
            formal_weights.weights_sha256
            if formal_weights is not None
            else _sha256(config.weights)
        )
        provenance_artifacts, parameters, sample_fingerprint = (
            _write_provenance_artifacts(
                config=config,
                output=output,
                prepared=prepared,
                model=model,
                model_config=model_config,
                weights_sha256=weights_sha256,
                compile_preflight=compile_preflight,
                formal_weights=formal_weights,
            )
        )

        identity = runtime_identity()
        git = git_provenance(config.repository_root)
        tags = cast(
            dict[str, str],
            sanitize_persisted_value(
                {
                    "dataset_fingerprint": prepared.composite.composite_fingerprint,
                    "split_fingerprint": prepared.split.split_fingerprint,
                    "source_run_id": str(payload["source_run_id"]),
                    "source_checkpoint_sha256": str(
                        payload["source_checkpoint_sha256"]
                    ),
                    "training_protocol_fingerprint": training_protocol_fingerprint,
                    "training_dataset_fingerprint": training_dataset_fingerprint,
                    "training_split_fingerprint": training_split_fingerprint,
                    "evaluation_dataset_fingerprint": (
                        prepared.composite.composite_fingerprint
                    ),
                    "evaluation_split_fingerprint": (prepared.split.split_fingerprint),
                    "external_evaluation_split": str(not lineage_matches).lower(),
                    "formal_training_weights_verified": str(
                        formal_weights is not None
                    ).lower(),
                    "git.branch": git.branch,
                    "git.commit": git.commit,
                    "git.dirty": str(git.dirty).lower(),
                    "host.name": identity["hostname"],
                    "python.executable": identity["python_executable"],
                    "machine_ids": json.dumps(
                        sorted({sample.machine_id for sample in prepared.samples})
                    ),
                }
            ),
        )
        logger.set_tags(tags)
        logger.log_params(parameters)
        for artifact, artifact_path in provenance_artifacts:
            logger.log_artifact(artifact, artifact_path=artifact_path)

        evaluated = evaluate_batches(
            forward,
            prepared.source,
            split=config.split,
            device=device,
            log_variance_offset=float(
                cast(float, payload["uncertainty_log_variance_offset"])
            ),
        )
        if config.compile_enabled:
            graph_break_count = max(
                0, torch_compile_graph_break_count() - graph_break_baseline
            )
            if graph_break_count != 0:
                raise RuntimeError(
                    "torch.compile evaluation observed "
                    f"{graph_break_count} graph breaks; eager fallback is forbidden"
                )

        prediction_records = tuple(
            Prediction(
                sample_id=sample_id,
                mean_volume_ul=float(mean),
                std_volume_ul=math.exp(0.5 * float(log_variance)),
            )
            for sample_id, mean, log_variance in zip(
                evaluated.sample_ids,
                evaluated.mean_volume_ul.flatten().tolist(),
                evaluated.log_variance_volume_ul2.flatten().tolist(),
                strict=True,
            )
        )
        by_id = {sample.sample_id: sample for sample in prepared.samples}
        try:
            evaluated_samples = tuple(
                by_id[sample_id] for sample_id in evaluated.sample_ids
            )
        except KeyError as error:
            raise ValueError(
                f"evaluation predictionにsample index外のIDがあります: {error.args[0]}"
            ) from error
        diagnostics = build_diagnostic_report(evaluated_samples, prediction_records)
        diagnostic_report_path = write_json_artifact(
            output / f"diagnostics-{config.split}.json", diagnostics.to_dict()
        )
        diagnostic_report_sha256 = _sha256(diagnostic_report_path)
        report_path = write_json_artifact(
            output / f"evaluation-{config.split}.json",
            {
                "kind": EVALUATION_REPORT_KIND,
                "schema_version": EVALUATION_REPORT_SCHEMA_VERSION,
                "run_id": run_id,
                "split": config.split,
                "diagnostic_only": config.diagnostic_only,
                "release_evidence": config.release_evidence,
                "external_evaluation_split": not lineage_matches,
                "metrics": asdict(evaluated.metrics),
                "sample_ids": list(evaluated.sample_ids),
                "mean_volume_ul": evaluated.mean_volume_ul.flatten().tolist(),
                "log_variance_volume_ul2": (
                    evaluated.log_variance_volume_ul2.flatten().tolist()
                ),
                "target_volume_ul": evaluated.target_volume_ul.flatten().tolist(),
                "predictions": [
                    {
                        "sample_id": item.sample_id,
                        "mean_volume_ul": item.mean_volume_ul,
                        "std_volume_ul": item.std_volume_ul,
                    }
                    for item in prediction_records
                ],
                "diagnostics": diagnostics.to_dict(),
                "diagnostic_report_sha256": diagnostic_report_sha256,
                "weights_sha256": weights_sha256,
                "formal_training_weights_verified": formal_weights is not None,
                "training_lineage": {
                    "run_id": str(payload["source_run_id"]),
                    "run_kind": str(payload["run_kind"]),
                    "source_checkpoint_sha256": str(
                        payload["source_checkpoint_sha256"]
                    ),
                    "dataset_fingerprint": training_dataset_fingerprint,
                    "split_fingerprint": training_split_fingerprint,
                    "training_protocol_fingerprint": training_protocol_fingerprint,
                    "formal_attestation_fingerprint": (
                        formal_weights.attestation.output_fingerprint
                        if formal_weights is not None
                        else None
                    ),
                },
                "evaluation_lineage": {
                    "dataset_fingerprint": (prepared.composite.composite_fingerprint),
                    "content_fingerprint": prepared.composite.content_fingerprint,
                    "sample_index_fingerprint": sample_fingerprint,
                    "split_fingerprint": prepared.split.split_fingerprint,
                },
            },
        )
        logger.log_metrics(evaluated.metrics.to_dict(f"{config.split}/"), step=0)
        diagnostic_metrics = {
            f"{config.split}/diagnostic/{key}": value
            for key, value in diagnostics.overall.to_dict().items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        logger.log_metrics(diagnostic_metrics, step=0)
        logger.log_artifact(report_path, artifact_path="evaluation")
        logger.log_artifact(diagnostic_report_path, artifact_path="evaluation")
        logger.flush()
        if formal_tracking_uri is None:
            logger.end(status="FINISHED")
        else:
            publish_formal_artifact(
                report_path,
                tracking_uri=formal_tracking_uri,
                run_kind="evaluate",
                logger=logger,
            )
        return EvaluationResult(
            run_id=run_id,
            metrics=evaluated.metrics,
            report_path=report_path,
            dataset_fingerprint=prepared.composite.composite_fingerprint,
            split_fingerprint=prepared.split.split_fingerprint,
            weights_sha256=weights_sha256,
            split=config.split,
            training_dataset_fingerprint=training_dataset_fingerprint,
            training_split_fingerprint=training_split_fingerprint,
            training_protocol_fingerprint=training_protocol_fingerprint,
            external_evaluation_split=not lineage_matches,
            diagnostic_only=config.diagnostic_only,
            release_evidence=config.release_evidence,
            predictions=prediction_records,
            diagnostics=diagnostics,
            diagnostic_report_path=diagnostic_report_path,
            diagnostic_report_sha256=diagnostic_report_sha256,
        )
    except Exception as error:
        _failure_close(logger, error)
        raise


def evaluate_config_from_mapping(
    raw: Mapping[str, object], *, base_directory: Path
) -> tuple[EvaluateConfig, LoggerConfig]:
    """Convert a resolved Hydra mapping into strict frozen evaluation
    config."""

    allowed = {
        "data",
        "logger",
        "weights",
        "output_directory",
        "split",
        "allow_frozen_test",
        "allow_external_split",
        "device",
        "compile_enabled",
        "compile_backend",
        "compile_mode",
        "repository_root",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown evaluate config keys: {sorted(unknown)}")
    for required in ("data", "logger", "weights", "output_directory"):
        if required not in raw:
            raise ValueError(f"evaluate config requires {required}")

    split_value = raw.get("split", "validation")
    if split_value not in ("validation", "test"):
        raise ValueError("split must be validation or test")
    for name in ("allow_frozen_test", "allow_external_split", "compile_enabled"):
        if name in raw and type(raw[name]) is not bool:
            raise TypeError(f"{name} must be a boolean")

    dummy = {
        "data": raw["data"],
        "model": PasteVolumeModelConfig().to_dict(),
        "trainer": TrainerConfig(max_epochs=1).to_dict(),
        "logger": raw["logger"],
        "checkpoint": {"directory": str(raw["output_directory"])},
        "run_kind": "base-train",
        "repository_root": raw.get("repository_root", str(base_directory)),
        "parent_base_run_id": None,
    }
    parsed_train, logger_config = train_config_from_mapping(
        dummy, base_directory=base_directory
    )

    def absolute(value: object) -> Path:
        path = Path(str(value)).expanduser()
        return (
            path.resolve() if path.is_absolute() else (base_directory / path).resolve()
        )

    return (
        EvaluateConfig(
            data=parsed_train.data,
            weights=absolute(raw["weights"]),
            output_directory=absolute(raw["output_directory"]),
            split=cast(Literal["validation", "test"], split_value),
            allow_frozen_test=cast(bool, raw.get("allow_frozen_test", False)),
            allow_external_split=cast(bool, raw.get("allow_external_split", False)),
            device=str(raw.get("device", "auto")),
            compile_enabled=cast(bool, raw.get("compile_enabled", True)),
            compile_backend=str(raw.get("compile_backend", "inductor")),
            compile_mode=str(raw.get("compile_mode", "default")),
            repository_root=parsed_train.repository_root,
        ),
        logger_config,
    )


def hydra_evaluate(raw_config: object) -> EvaluationResult:
    """Run the formal evaluation path from Hydra's resolved config."""

    from hydra.core.hydra_config import HydraConfig
    from omegaconf import OmegaConf

    resolved = OmegaConf.to_container(raw_config, resolve=True, throw_on_missing=True)
    if not isinstance(resolved, dict):
        raise ValueError("resolved Hydra evaluate config must be a mapping")
    base = Path(HydraConfig.get().runtime.cwd)
    config, logger_config = evaluate_config_from_mapping(
        cast(Mapping[str, object], resolved), base_directory=base
    )
    persisted_resolved = cast(dict[str, object], sanitize_persisted_value(resolved))
    config = replace(
        config,
        hydra_resolved_yaml=OmegaConf.to_yaml(
            OmegaConf.create(persisted_resolved), resolve=True
        ),
        hydra_overrides=tuple(
            sanitize_persisted_text(item) for item in HydraConfig.get().overrides.task
        ),
    )
    logger = MLflowExperimentLogger(
        tracking_uri=logger_config.tracking_uri,
        experiment_name=sanitize_persisted_text(logger_config.experiment_name),
        run_name=(
            sanitize_persisted_text(logger_config.run_name)
            if logger_config.run_name is not None
            else None
        ),
        metric_retry_count=logger_config.metric_retry_count,
    )
    return evaluate(
        config,
        logger,
        formal_tracking_uri=logger_config.tracking_uri,
    )


def main() -> None:
    """``python -m pcbasm.pasting.paste_volume.evaluate`` entrypoint."""

    import hydra

    @hydra.main(version_base="1.3", config_path="conf", config_name="evaluate")
    def run(config: object) -> None:
        result = hydra_evaluate(config)
        print(json.dumps({"run_id": result.run_id, "report": str(result.report_path)}))

    run()


if __name__ == "__main__":
    main()


__all__ = [
    "EVALUATION_REPORT_KIND",
    "EVALUATION_REPORT_SCHEMA_VERSION",
    "EvaluateConfig",
    "EvaluationResult",
    "evaluate",
    "evaluate_config_from_mapping",
    "hydra_evaluate",
]
