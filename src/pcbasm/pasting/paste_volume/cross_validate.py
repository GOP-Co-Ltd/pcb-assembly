"""Hydra/MLflow entrypoint for formal cross-group validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from .cross_validation import (
    CrossValidationConfig,
    CrossValidationLoggerFactory,
    CrossValidationResult,
    MLflowCrossValidationLoggerFactory,
    run_cross_validation,
)
from .data import (
    DatasetValidationReport,
    build_sample_index,
    resolve_dataset_inputs,
    save_composite_manifest,
    save_sample_index,
)
from .dependencies import dependency_versions, git_provenance
from .experiment import ExperimentLogger, MLflowExperimentLogger, write_json_artifact
from .formal_artifact import (
    FormalArtifactAttestation,
    publish_formal_artifact,
)
from .reporting import CrossGroupDimension
from .train import (
    LoggerConfig,
    sanitize_persisted_text,
    sanitize_persisted_uri,
    sanitize_persisted_value,
    summarize_persisted_git_diff,
    train_config_from_mapping,
    training_protocol_fingerprint,
)


@dataclass(frozen=True)
class FormalCrossValidationResult:
    result: CrossValidationResult
    summary_run_id: str
    resolved_config_path: Path
    resolved_config_sha256: str
    attestation: FormalArtifactAttestation


def _absolute_path(value: object, base_directory: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cross_validation output_directory must be a non-empty string")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base_directory / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def cross_validation_config_from_mapping(
    raw: Mapping[str, object],
    *,
    base_directory: Path,
    hydra_resolved_yaml: str | None = None,
    hydra_overrides: Sequence[str] = (),
) -> tuple[CrossValidationConfig, LoggerConfig]:
    """Parse one resolved Hydra config with strict unknown-key rejection."""

    training_keys = {
        "data",
        "model",
        "trainer",
        "logger",
        "checkpoint",
        "run_kind",
        "repository_root",
        "parent_base_run_id",
        "hpo",
    }
    unknown = set(raw) - training_keys - {"cross_validation"}
    if unknown:
        raise ValueError(f"unknown cross-validation config keys: {sorted(unknown)}")
    if "cross_validation" not in raw:
        raise ValueError("cross_validation config is required")
    training, logger = train_config_from_mapping(
        {key: raw[key] for key in training_keys if key in raw},
        base_directory=base_directory,
    )
    if hydra_resolved_yaml is None and hydra_overrides:
        raise ValueError("Hydra overrides require a resolved Hydra config")
    training = replace(
        training,
        hydra_resolved_yaml=(
            sanitize_persisted_text(hydra_resolved_yaml)
            if hydra_resolved_yaml is not None
            else None
        ),
        hydra_overrides=tuple(
            sanitize_persisted_text(item) for item in hydra_overrides
        ),
    )
    logger = replace(
        logger,
        experiment_name=sanitize_persisted_text(logger.experiment_name),
        run_name=(
            sanitize_persisted_text(logger.run_name)
            if logger.run_name is not None
            else None
        ),
    )
    if training.data.split_manifest is not None:
        raise ValueError(
            "cross-validation must construct fold splits; split_manifest is forbidden"
        )
    cross_value = raw["cross_validation"]
    if not isinstance(cross_value, Mapping):
        raise ValueError("cross_validation config must be a mapping")
    cross_raw = cast(Mapping[str, object], cross_value)
    allowed = {
        "output_directory",
        "dimension",
        "seed",
        "evaluation_device",
        "evaluation_compile_enabled",
    }
    if cross_unknown := set(cross_raw) - allowed:
        raise ValueError(f"unknown cross_validation keys: {sorted(cross_unknown)}")
    if "output_directory" not in cross_raw or "dimension" not in cross_raw:
        raise ValueError("cross_validation output_directory and dimension are required")
    dimension = cross_raw["dimension"]
    if not isinstance(dimension, str) or dimension not in (
        "machine",
        "paste_lot",
        "nozzle",
    ):
        raise ValueError("cross_validation dimension is invalid")
    seed = cross_raw.get("seed", 42)
    if type(seed) is not int or seed < 0:
        raise ValueError("cross_validation seed must be a non-negative integer")
    evaluation_device = cross_raw.get("evaluation_device", "auto")
    if not isinstance(evaluation_device, str):
        raise ValueError("evaluation_device must be a string")
    evaluation_compile_enabled = cross_raw.get("evaluation_compile_enabled", True)
    if type(evaluation_compile_enabled) is not bool:
        raise ValueError("evaluation_compile_enabled must be bool")
    return (
        CrossValidationConfig(
            training=training,
            output_directory=_absolute_path(
                cross_raw["output_directory"], base_directory
            ),
            dimension=cast(CrossGroupDimension, dimension),
            seed=seed,
            evaluation_device=evaluation_device,
            evaluation_compile_enabled=evaluation_compile_enabled,
            protocol_fingerprint=None,
        ),
        logger,
    )


def _resolved_config_payload(
    config: CrossValidationConfig, logger: LoggerConfig
) -> dict[str, object]:
    return cast(
        dict[str, object],
        sanitize_persisted_value(
            {
                "training": config.training.to_dict(),
                "logger": {
                    "tracking_uri": sanitize_persisted_uri(logger.tracking_uri),
                    "experiment_name": logger.experiment_name,
                    "run_name": logger.run_name,
                    "metric_retry_count": logger.metric_retry_count,
                },
                "cross_validation": {
                    "output_directory": str(config.output_directory),
                    "dimension": config.dimension,
                    "seed": config.seed,
                    "evaluation_device": config.evaluation_device,
                    "evaluation_compile_enabled": config.evaluation_compile_enabled,
                    "training_protocol_fingerprint": training_protocol_fingerprint(
                        config.training
                    ),
                },
            }
        ),
    )


def _write_summary_provenance(
    config: CrossValidationConfig,
    result: CrossValidationResult,
) -> tuple[tuple[Path, str], ...]:
    composite = resolve_dataset_inputs(
        manifest=config.training.data.manifest,
        roots=config.training.data.roots,
    )
    samples = build_sample_index(composite)
    sample_ids = tuple(sorted(sample.sample_id for sample in samples))
    if (
        composite.composite_fingerprint != result.composite_fingerprint
        or sample_ids != result.dataset_sample_ids
    ):
        raise RuntimeError(
            "cross-validation dataset changed before summary provenance was recorded"
        )

    directory = config.output_directory / "summary-provenance"
    directory.mkdir(parents=True, exist_ok=False)
    composite_path = directory / "composite.json"
    sample_index_path = directory / "sample-index.json"
    validation_path = directory / "dataset-validation.json"
    dependencies_path = directory / "dependencies.json"
    git_path = directory / "git.json"
    git_diff_path = directory / "git.diff"
    save_composite_manifest(composite, composite_path)
    save_sample_index(samples, sample_index_path)
    write_json_artifact(
        validation_path,
        DatasetValidationReport(
            composite_fingerprint=composite.composite_fingerprint,
            content_fingerprint=composite.content_fingerprint,
            source_count=len(composite.sources),
            session_count=len(composite.sessions),
            sample_count=len(samples),
        ).to_dict(),
    )
    write_json_artifact(dependencies_path, dependency_versions())
    provenance = git_provenance(config.training.repository_root)
    persisted_diff = summarize_persisted_git_diff(provenance.diff)
    git_diff_path.write_text(persisted_diff + "\n", encoding="utf-8")
    persisted_git = provenance.to_dict()
    persisted_git["diff"] = persisted_diff
    persisted_git["untracked_content"] = "[omitted at persistence boundary]"
    write_json_artifact(
        git_path,
        cast(
            Mapping[str, object],
            sanitize_persisted_value(persisted_git),
        ),
    )
    paths = [
        composite_path,
        sample_index_path,
        validation_path,
        dependencies_path,
        git_path,
        git_diff_path,
    ]
    if config.training.hydra_resolved_yaml is not None:
        resolved_yaml_path = directory / "resolved-config.yaml"
        resolved_yaml_path.write_text(
            sanitize_persisted_text(config.training.hydra_resolved_yaml),
            encoding="utf-8",
        )
        overrides_path = directory / "hydra-overrides.json"
        write_json_artifact(
            overrides_path,
            {
                "overrides": [
                    sanitize_persisted_text(item)
                    for item in config.training.hydra_overrides
                ]
            },
        )
        paths.extend((resolved_yaml_path, overrides_path))
    return tuple((path, "cross-validation/provenance") for path in paths)


def _end_failed_summary(
    logger: ExperimentLogger,
    *,
    started: bool,
    tags: Mapping[str, str],
    reason: str,
) -> None:
    if not started:
        try:
            logger.start(run_kind="cross-validation-summary", tags=tags)
            started = True
        except Exception:
            return
    try:
        logger.set_tags({"failure_reason": reason})
    except Exception:
        pass
    try:
        logger.end(status="FAILED")
    except Exception:
        pass


def run_formal_cross_validation(
    config: CrossValidationConfig,
    logger_config: LoggerConfig,
    logger_factory: CrossValidationLoggerFactory,
    summary_logger: ExperimentLogger,
) -> FormalCrossValidationResult:
    """Run all folds and commit their aggregate report to a formal summary
    run."""

    protocol_fingerprint = training_protocol_fingerprint(config.training)
    try:
        result = run_cross_validation(config, logger_factory)
    except Exception:
        _end_failed_summary(
            summary_logger,
            started=False,
            tags={
                "cross_group_dimension": config.dimension,
                "training_protocol_fingerprint": protocol_fingerprint,
                "cross_validation_status": "failed",
            },
            reason="cross_validation_failed",
        )
        raise

    resolved_config_path = config.output_directory / "resolved-config.json"
    report_sha256 = _sha256(result.report_path)
    summary_tags = {
        "cross_group_dimension": result.dimension,
        "dataset_fingerprint": result.composite_fingerprint,
        "training_protocol_fingerprint": result.protocol_fingerprint,
        "cross_validation_report_fingerprint": result.report_fingerprint,
        "cross_validation_report_sha256": report_sha256,
        "cross_validation_available": str(result.available).lower(),
        "release_blocking": str(not result.available).lower(),
    }
    summary_started = False
    try:
        write_json_artifact(
            resolved_config_path, _resolved_config_payload(config, logger_config)
        )
        provenance_artifacts = _write_summary_provenance(config, result)
        resolved_config_sha256 = _sha256(resolved_config_path)
        summary_run_id = summary_logger.start(
            run_kind="cross-validation-summary", tags=summary_tags
        )
        summary_started = True
        summary_logger.log_params(
            {
                "dimension": result.dimension,
                "seed": config.seed,
                "fold_count": len(result.folds),
                "sample_count": len(result.dataset_sample_ids),
                "report_fingerprint": result.report_fingerprint,
                "report_sha256": report_sha256,
                "resolved_config_sha256": resolved_config_sha256,
            }
        )
        if result.diagnostics is not None:
            metrics = {
                f"cross_validation/{key}": float(value)
                for key, value in result.diagnostics.overall.to_dict().items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            summary_logger.log_metrics(metrics, step=0)
        summary_logger.log_artifact(
            result.report_path, artifact_path="cross-validation"
        )
        summary_logger.log_artifact(
            resolved_config_path, artifact_path="cross-validation"
        )
        for path, artifact_path in provenance_artifacts:
            summary_logger.log_artifact(path, artifact_path=artifact_path)
        for fold in result.folds:
            fold_artifact_path = f"cross-validation/folds/{fold.fold_id}"
            for path in (
                fold.split_path,
                fold.evaluation_report_path,
                fold.diagnostic_report_path,
            ):
                summary_logger.log_artifact(path, artifact_path=fold_artifact_path)
        attestation = publish_formal_artifact(
            result.report_path,
            tracking_uri=logger_config.tracking_uri,
            run_kind="cross-validation-summary",
            logger=summary_logger,
        )
    except Exception:
        _end_failed_summary(
            summary_logger,
            started=summary_started,
            tags=summary_tags,
            reason="summary_logging_failed",
        )
        raise
    return FormalCrossValidationResult(
        result=result,
        summary_run_id=summary_run_id,
        resolved_config_path=resolved_config_path,
        resolved_config_sha256=resolved_config_sha256,
        attestation=attestation,
    )


def hydra_cross_validate(raw_config: object) -> FormalCrossValidationResult:
    """Resolve Hydra once, then run the strict cross-validation boundary."""

    from hydra.core.hydra_config import HydraConfig
    from omegaconf import OmegaConf

    resolved = OmegaConf.to_container(raw_config, resolve=True, throw_on_missing=True)
    if not isinstance(resolved, dict):
        raise ValueError("resolved Hydra cross-validation config must be a mapping")
    base = (
        Path(HydraConfig.get().runtime.cwd) if HydraConfig.initialized() else Path.cwd()
    )
    persisted_resolved = cast(dict[str, object], sanitize_persisted_value(resolved))
    persisted_overrides = (
        tuple(
            sanitize_persisted_text(item) for item in HydraConfig.get().overrides.task
        )
        if HydraConfig.initialized()
        else ()
    )
    config, logger_config = cross_validation_config_from_mapping(
        cast(Mapping[str, object], resolved),
        base_directory=base,
        hydra_resolved_yaml=OmegaConf.to_yaml(
            OmegaConf.create(persisted_resolved), resolve=True
        ),
        hydra_overrides=persisted_overrides,
    )
    factory = MLflowCrossValidationLoggerFactory(logger_config)
    summary_logger = MLflowExperimentLogger(
        tracking_uri=logger_config.tracking_uri,
        experiment_name=logger_config.experiment_name,
        run_name=(
            f"{logger_config.run_name}-summary"
            if logger_config.run_name is not None
            else f"paste-volume-cross-validation-{config.dimension}-summary"
        ),
        metric_retry_count=logger_config.metric_retry_count,
    )
    return run_formal_cross_validation(config, logger_config, factory, summary_logger)


def main() -> None:
    """`python -m pcbasm.pasting.paste_volume.cross_validate` entrypoint."""

    import hydra

    @hydra.main(version_base="1.3", config_path="conf", config_name="cross_validate")
    def run(config: object) -> None:
        execution = hydra_cross_validate(config)
        print(
            json.dumps(
                {
                    "summary_run_id": execution.summary_run_id,
                    "report": str(execution.result.report_path),
                    "available": execution.result.available,
                }
            )
        )

    run()


if __name__ == "__main__":
    main()


__all__ = [
    "FormalCrossValidationResult",
    "cross_validation_config_from_mapping",
    "hydra_cross_validate",
    "run_formal_cross_validation",
]
