"""Paste-volume学習のfrozen config境界とHydra entrypoint."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

import attrs

from .data import (
    AugmentationConfig,
    CompositeDataset,
    DatasetValidationReport,
    ImageConstraints,
    PasteVolumeBatch,
    PasteVolumeDataset,
    PasteVolumeSample,
    PixelBudgetBatchSampler,
    SplitManifest,
    build_sample_index,
    collate_paste_volume,
    create_session_split,
    index_samples_by_id,
    load_split_manifest,
    resolve_dataset_inputs,
    sample_index_fingerprint,
    save_composite_manifest,
    save_sample_index,
    save_split_manifest,
    select_session_balanced_samples,
    validate_split_manifest,
)
from .dependencies import dependency_versions, git_provenance, runtime_identity
from .experiment import ExperimentLogger, MLflowExperimentLogger, write_json_artifact
from .model import PasteVolumeModelConfig
from .training import (
    CheckpointConfig,
    TrainerConfig,
    TrainingBatch,
    TrainingCoreConfig,
    TrainingData,
    TrainResult,
    train_model,
)

_URI_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"']+")


def _sanitize_malformed_uri(uri: str) -> str:
    """Best-effortでuserinfoと接続optionを除き、例外を外へ出さない."""

    scheme, separator, remainder = uri.partition("://")
    if not separator:
        return uri
    authority_end = len(remainder)
    for delimiter in ("/", "?", "#"):
        position = remainder.find(delimiter)
        if position >= 0:
            authority_end = min(authority_end, position)
    authority = remainder[:authority_end].rsplit("@", maxsplit=1)[-1]
    suffix = remainder[authority_end:]
    if not suffix.startswith("/"):
        suffix = ""
    suffix = suffix.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    return f"{scheme}://{authority}{suffix}"


@dataclass(frozen=True)
class DataConfig:
    manifest: Path | None = None
    roots: tuple[Path, ...] = ()
    split_manifest: Path | None = None
    split_seed: int = 42
    train_ratio: float = 0.70
    validation_ratio: float = 0.15
    test_ratio: float = 0.15
    constraints: ImageConstraints = field(default_factory=ImageConstraints)
    augmentation: AugmentationConfig = field(default_factory=AugmentationConfig)
    max_batch_pixels: int = 8_388_608
    max_batch_size: int = 32

    def __post_init__(self) -> None:
        if self.manifest is not None and self.roots:
            raise ValueError("data.manifest and data.roots are mutually exclusive")
        if self.manifest is None and not self.roots:
            raise ValueError("data.manifest or at least one data.root is required")
        if self.max_batch_pixels < 1 or self.max_batch_size < 1:
            raise ValueError("batch limits must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "manifest": str(self.manifest) if self.manifest is not None else None,
            "roots": [str(root) for root in self.roots],
            "split_manifest": (
                str(self.split_manifest) if self.split_manifest is not None else None
            ),
            "constraints": self.constraints.to_dict(),
            "augmentation": attrs.asdict(self.augmentation),
        }


def sanitize_persisted_uri(uri: str) -> str:
    """URIからcredentialと接続optionを除いた永続化可能なidentityを返す."""

    try:
        parsed = urlsplit(uri)
        if not parsed.scheme:
            return uri
        if parsed.hostname is None:
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return _sanitize_malformed_uri(uri)


def _sanitize_persisted_text(value: str) -> str:
    return _URI_PATTERN.sub(lambda match: sanitize_persisted_uri(match.group(0)), value)


def _sanitize_persisted_value(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_persisted_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_persisted_value(item) for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_sanitize_persisted_value(item) for item in value)
    if isinstance(value, list):
        return [_sanitize_persisted_value(item) for item in value]
    return value


def sanitize_persisted_text(value: str) -> str:
    """Artifact/log境界でURI credential/optionsを除去する."""

    return _sanitize_persisted_text(value)


def sanitize_persisted_value(value: object) -> object:
    """Nested artifact/log payloadの全文字列を安全にする."""

    return _sanitize_persisted_value(value)


def summarize_persisted_git_diff(diff: str) -> str:
    """任意のsecretを含み得るdiff本文を永続化せず、同一性だけを残す."""

    encoded = diff.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return (
        "[omitted at persistence boundary; " f"sha256:{digest}; bytes:{len(encoded)}]"
    )


@dataclass(frozen=True)
class LoggerConfig:
    tracking_uri: str = "http://127.0.0.1:5000"
    experiment_name: str = "paste-volume"
    run_name: str | None = None
    metric_retry_count: int = 3

    def __post_init__(self) -> None:
        if urlsplit(self.tracking_uri).scheme not in ("http", "https"):
            raise ValueError("formal MLflow tracking_uri must use HTTP or HTTPS")
        if not self.experiment_name:
            raise ValueError("MLflow experiment_name is required")
        if self.metric_retry_count < 1:
            raise ValueError("metric_retry_count must be positive")


@dataclass(frozen=True)
class HpoTrialConfig:
    study_name: str
    trial_number: int
    search_config_fingerprint: str
    storage_uri_redacted: str

    def __post_init__(self) -> None:
        if not self.study_name or not self.search_config_fingerprint:
            raise ValueError("HPO study name and search fingerprint are required")
        if self.trial_number < 0:
            raise ValueError("HPO trial_number must be non-negative")
        if sanitize_persisted_uri(self.storage_uri_redacted) != (
            self.storage_uri_redacted
        ):
            raise ValueError("HPO storage_uri_redacted contains sensitive URI data")


@dataclass(frozen=True)
class TrainConfig:
    data: DataConfig
    checkpoint: CheckpointConfig
    model: PasteVolumeModelConfig = field(default_factory=PasteVolumeModelConfig)
    trainer: TrainerConfig = field(default_factory=TrainerConfig)
    run_kind: Literal["base-train", "finetune"] = "base-train"
    repository_root: Path = Path(".")
    parent_base_run_id: str | None = None
    hpo: HpoTrialConfig | None = None
    hydra_resolved_yaml: str | None = None
    hydra_overrides: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.run_kind == "base-train"
            and self.checkpoint.initial_weights is not None
        ):
            raise ValueError("base training cannot use initial weights")
        if (
            self.run_kind == "finetune"
            and self.checkpoint.initial_weights is None
            and self.checkpoint.resume_checkpoint is None
        ):
            raise ValueError("new finetune run requires initial weights")

    def to_dict(self) -> dict[str, object]:
        return {
            "data": self.data.to_dict(),
            "checkpoint": {
                "directory": str(self.checkpoint.directory),
                "resume_checkpoint": (
                    str(self.checkpoint.resume_checkpoint)
                    if self.checkpoint.resume_checkpoint is not None
                    else None
                ),
                "initial_weights": (
                    str(self.checkpoint.initial_weights)
                    if self.checkpoint.initial_weights is not None
                    else None
                ),
                "save_interval_steps": self.checkpoint.save_interval_steps,
                "save_interval_seconds": self.checkpoint.save_interval_seconds,
            },
            "model": self.model.to_dict(),
            "trainer": self.trainer.to_dict(),
            "run_kind": self.run_kind,
            "repository_root": str(self.repository_root),
            "parent_base_run_id": self.parent_base_run_id,
            "hpo": asdict(self.hpo) if self.hpo is not None else None,
        }


@dataclass(frozen=True)
class PreparedTrainingData:
    composite: CompositeDataset
    samples: tuple[PasteVolumeSample, ...]
    split: SplitManifest
    selected_train_samples: tuple[PasteVolumeSample, ...]
    source: TrainingData


class _PasteVolumeTrainingData:
    def __init__(
        self,
        samples: Sequence[PasteVolumeSample],
        split: SplitManifest,
        selected_train_samples: Sequence[PasteVolumeSample],
        config: DataConfig,
        *,
        seed: int,
    ) -> None:
        self._constraints = config.constraints
        self._train_samples = tuple(selected_train_samples)
        by_id = index_samples_by_id(samples)
        self._evaluation_samples = {
            "validation": tuple(by_id[item] for item in split.validation_sample_ids),
            "test": tuple(by_id[item] for item in split.test_sample_ids),
        }
        self._train_dataset = PasteVolumeDataset(
            self._train_samples,
            constraints=config.constraints,
            training=True,
            global_seed=seed,
            augmentation=config.augmentation,
        )
        self._evaluation_datasets = {
            name: PasteVolumeDataset(
                current,
                constraints=config.constraints,
                training=False,
                global_seed=seed,
                augmentation=config.augmentation,
            )
            for name, current in self._evaluation_samples.items()
        }
        self._train_index = {
            sample.sample_id: index for index, sample in enumerate(self._train_samples)
        }
        self._evaluation_indices = {
            name: {sample.sample_id: index for index, sample in enumerate(current)}
            for name, current in self._evaluation_samples.items()
        }
        self._train_sampler = PixelBudgetBatchSampler(
            self._train_samples,
            max_batch_pixels=config.max_batch_pixels,
            max_batch_size=config.max_batch_size,
            constraints=config.constraints,
            training=True,
            global_seed=seed,
            augmentation=config.augmentation,
        )
        self._evaluation_samplers = {
            name: PixelBudgetBatchSampler(
                current,
                max_batch_pixels=config.max_batch_pixels,
                max_batch_size=config.max_batch_size,
                constraints=config.constraints,
                training=False,
                global_seed=seed,
                augmentation=config.augmentation,
            )
            for name, current in self._evaluation_samples.items()
            if current
        }

    @staticmethod
    def _convert(batch: PasteVolumeBatch) -> TrainingBatch:
        return TrainingBatch(
            image_6ch=batch.image_6ch,
            valid_pixel_mask=batch.valid_pixel_mask,
            pixel_per_mm=batch.pixel_per_mm,
            target_volume_ul=batch.target_volume_ul.reshape(-1, 1),
            sample_weight=batch.loss_weight.reshape(-1, 1),
            sample_ids=batch.sample_ids,
        )

    def training_batch_plan(self, epoch: int) -> tuple[tuple[str, ...], ...]:
        index_plan = self._train_sampler.plan_for_epoch(epoch)
        return tuple(
            tuple(self._train_samples[index].sample_id for index in batch)
            for batch in index_plan
        )

    def training_batch(
        self, sample_ids: tuple[str, ...], *, epoch: int
    ) -> TrainingBatch:
        self._train_dataset.set_epoch(epoch)
        items = [self._train_dataset[self._train_index[item]] for item in sample_ids]
        return self._convert(
            collate_paste_volume(items, training=True, stride=self._constraints.stride)
        )

    def evaluation_batch_plan(
        self, split: Literal["validation", "test"]
    ) -> tuple[tuple[str, ...], ...]:
        sampler = self._evaluation_samplers.get(split)
        if sampler is None:
            return ()
        samples = self._evaluation_samples[split]
        return tuple(
            tuple(samples[index].sample_id for index in batch)
            for batch in sampler.plan_for_epoch(0)
        )

    def evaluation_batch(
        self,
        sample_ids: tuple[str, ...],
        *,
        split: Literal["validation", "test"],
    ) -> TrainingBatch:
        dataset = self._evaluation_datasets[split]
        indices = self._evaluation_indices[split]
        items = [dataset[indices[item]] for item in sample_ids]
        return self._convert(
            collate_paste_volume(items, training=False, stride=self._constraints.stride)
        )


def _canonical_fingerprint(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def training_protocol_fingerprint(config: TrainConfig) -> str:
    """Dataset assignmentや実行pathに依存しない学習protocol正典を返す."""

    return _canonical_fingerprint(
        {
            "schema_version": 1,
            "run_kind": config.run_kind,
            "model": config.model.to_dict(),
            "preprocess_and_batching": {
                "constraints": config.data.constraints.to_dict(),
                "augmentation": attrs.asdict(config.data.augmentation),
                "max_batch_pixels": config.data.max_batch_pixels,
                "max_batch_size": config.data.max_batch_size,
            },
            "trainer": config.trainer.to_dict(),
        }
    )


def preprocess_schema(constraints: ImageConstraints) -> dict[str, object]:
    """train/export/runtimeで共有するv1前処理schemaを返す."""

    return {
        "schema_version": 1,
        "channel_order": "RGB",
        "input_channels": 6,
        "normalization": {
            "kind": "sample-layer-norm",
            "axes": [0, 1, 2],
            "affine": False,
            "per_channel": False,
            "epsilon": constraints.normalization_epsilon,
        },
        "image_constraints": constraints.to_dict(),
    }


def prepare_training_data(
    config: TrainConfig,
    *,
    split_override: SplitManifest | None = None,
) -> PreparedTrainingData:
    """Datasetを解決し、splitとdeterministic batch sourceを固定する."""

    composite = resolve_dataset_inputs(
        manifest=config.data.manifest, roots=config.data.roots
    )
    samples = build_sample_index(composite)
    if split_override is not None and config.data.split_manifest is not None:
        raise ValueError(
            "split_override and data.split_manifest are mutually exclusive"
        )
    if split_override is not None:
        split = split_override
    elif config.data.split_manifest is not None:
        split = load_split_manifest(
            config.data.split_manifest,
            expected_composite_fingerprint=composite.composite_fingerprint,
        )
    else:
        split = create_session_split(
            samples,
            composite.composite_fingerprint,
            seed=config.data.split_seed,
            train_ratio=config.data.train_ratio,
            validation_ratio=config.data.validation_ratio,
            test_ratio=config.data.test_ratio,
            mode="base" if config.run_kind == "base-train" else "finetune",
        )
    validate_split_manifest(
        split,
        samples,
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    by_id = index_samples_by_id(samples)
    selected = tuple(by_id[item] for item in split.train_sample_ids)
    if (
        config.trainer.max_train_samples is not None
        and len(selected) > config.trainer.max_train_samples
    ):
        selected = select_session_balanced_samples(
            samples,
            split.train_sample_ids,
            limit=config.trainer.max_train_samples,
            seed=config.trainer.seed,
        )
    source = _PasteVolumeTrainingData(
        samples,
        split,
        selected,
        config.data,
        seed=config.trainer.seed,
    )
    return PreparedTrainingData(
        composite=composite,
        samples=samples,
        split=split,
        selected_train_samples=selected,
        source=source,
    )


def _write_run_artifacts(
    config: TrainConfig,
    prepared: PreparedTrainingData,
) -> tuple[tuple[Path, str | None], ...]:
    directory = config.checkpoint.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    composite_path = directory / "composite.json"
    sample_index_path = directory / "sample-index.json"
    split_path = directory / "split.json"
    config_path = directory / "config.json"
    dependency_path = directory / "dependencies.json"
    dataset_validation_path = directory / "dataset-validation.json"
    git_diff_path = directory / "git.diff"
    selected_path = directory / "selected-train-samples.json"
    dataset_validation = DatasetValidationReport(
        composite_fingerprint=prepared.composite.composite_fingerprint,
        content_fingerprint=prepared.composite.content_fingerprint,
        source_count=len(prepared.composite.sources),
        session_count=len(prepared.composite.sessions),
        sample_count=len(prepared.samples),
    ).to_dict()
    artifact_paths_list = [
        composite_path,
        sample_index_path,
        split_path,
        config_path,
        dependency_path,
        dataset_validation_path,
        git_diff_path,
        directory / "git.json",
        selected_path,
    ]
    if config.hydra_resolved_yaml is not None:
        artifact_paths_list.extend(
            (directory / "resolved-config.yaml", directory / "hydra-overrides.json")
        )
    artifact_paths = tuple(artifact_paths_list)
    if config.checkpoint.resume_checkpoint is not None:
        missing = [path for path in artifact_paths if not path.is_file()]
        if missing:
            raise ValueError(f"resume run artifacts are missing: {missing}")
        expected_json = {
            composite_path: prepared.composite.to_dict(),
            sample_index_path: {
                "kind": "pcbasm-paste-volume-sample-index",
                "schema_version": 1,
                "sample_index_fingerprint": sample_index_fingerprint(prepared.samples),
                "samples": [sample.to_dict() for sample in prepared.samples],
            },
            split_path: prepared.split.to_dict(),
            selected_path: {
                "sample_ids": [
                    sample.sample_id for sample in prepared.selected_train_samples
                ],
                "count": len(prepared.selected_train_samples),
            },
            dataset_validation_path: dataset_validation,
        }
        for path, expected in expected_json.items():
            actual = json.loads(path.read_text(encoding="utf-8"))
            if actual != expected:
                raise ValueError(f"resume run artifact does not match: {path}")
        return tuple((path, "provenance") for path in artifact_paths)
    save_composite_manifest(prepared.composite, composite_path)
    save_sample_index(prepared.samples, sample_index_path)
    save_split_manifest(prepared.split, split_path)
    write_json_artifact(
        config_path,
        cast(Mapping[str, object], _sanitize_persisted_value(config.to_dict())),
    )
    if config.hydra_resolved_yaml is not None:
        (directory / "resolved-config.yaml").write_text(
            _sanitize_persisted_text(config.hydra_resolved_yaml), encoding="utf-8"
        )
        write_json_artifact(
            directory / "hydra-overrides.json",
            {
                "overrides": [
                    _sanitize_persisted_text(item) for item in config.hydra_overrides
                ]
            },
        )
    write_json_artifact(dependency_path, dependency_versions())
    write_json_artifact(
        dataset_validation_path,
        dataset_validation,
    )
    write_json_artifact(
        selected_path,
        {
            "sample_ids": [
                sample.sample_id for sample in prepared.selected_train_samples
            ],
            "count": len(prepared.selected_train_samples),
        },
    )
    provenance = git_provenance(config.repository_root)
    persisted_diff = summarize_persisted_git_diff(provenance.diff)
    git_diff_path.write_text(persisted_diff + "\n", encoding="utf-8")
    persisted_git = provenance.to_dict()
    persisted_git["diff"] = persisted_diff
    # Untracked source is base64-encoded by the collector, so a textual URI
    # sanitizer cannot prove that credentials were removed.  File identity is
    # retained, while raw untracked content stays outside persisted ML artifacts.
    persisted_git["untracked_content"] = "[omitted at persistence boundary]"
    write_json_artifact(
        directory / "git.json",
        cast(Mapping[str, object], _sanitize_persisted_value(persisted_git)),
    )
    return tuple((path, "provenance") for path in artifact_paths)


def train(
    config: TrainConfig,
    logger: ExperimentLogger,
    *,
    split_override: SplitManifest | None = None,
    formal_tracking_uri: str | None = None,
) -> TrainResult:
    """通常Python APIとしてformal trainingを実行する."""

    entry_started_at = time.monotonic()
    prepared = prepare_training_data(config, split_override=split_override)
    artifacts = _write_run_artifacts(config, prepared)
    config_payload = cast(
        dict[str, object], _sanitize_persisted_value(config.to_dict())
    )
    config_payload.pop("hydra_resolved_yaml", None)
    config_payload.pop("hydra_overrides", None)
    checkpoint_payload = cast(dict[str, object], config_payload["checkpoint"])
    checkpoint_payload["resume_checkpoint"] = None
    # Resume identity is carried by the checkpoint's strict parent lineage.  Do not
    # make continued training depend on the original weights file still existing.
    checkpoint_payload["initial_weights"] = None
    config_payload["selected_train_sample_ids"] = [
        sample.sample_id for sample in prepared.selected_train_samples
    ]
    provenance = git_provenance(config.repository_root)
    identity = runtime_identity()
    lineage_tags = cast(
        dict[str, str],
        _sanitize_persisted_value(
            {
                "git.branch": provenance.branch,
                "git.commit": provenance.commit,
                "git.dirty": str(provenance.dirty).lower(),
                "host.name": identity["hostname"],
                "python.executable": identity["python_executable"],
                "machine_ids": json.dumps(
                    sorted({sample.machine_id for sample in prepared.samples})
                ),
            }
        ),
    )
    if config.hpo is not None:
        lineage_tags.update(
            {
                "hpo.study_name": config.hpo.study_name,
                "hpo.trial_number": str(config.hpo.trial_number),
                "hpo.search_config_fingerprint": config.hpo.search_config_fingerprint,
                "hpo.storage_uri": config.hpo.storage_uri_redacted,
            }
        )
    core_config = TrainingCoreConfig(
        run_kind=config.run_kind,
        model=config.model,
        trainer=config.trainer,
        checkpoint=config.checkpoint,
        dataset_fingerprint=prepared.composite.composite_fingerprint,
        split_fingerprint=prepared.split.split_fingerprint,
        config_fingerprint=_canonical_fingerprint(config_payload),
        training_protocol_fingerprint=training_protocol_fingerprint(config),
        preprocess_schema=preprocess_schema(config.data.constraints),
        train_sample_ids=tuple(
            sample.sample_id for sample in prepared.selected_train_samples
        ),
        parent_base_run_id=config.parent_base_run_id,
        run_tags=lineage_tags,
        run_params=_flatten_run_parameters(config),
        resolved_config=config_payload,
    )
    return train_model(
        core_config,
        prepared.source,
        logger,
        run_artifacts=artifacts,
        deadline_started_at=entry_started_at,
        formal_tracking_uri=formal_tracking_uri,
    )


def _flatten_run_parameters(config: TrainConfig) -> dict[str, str | int | float | bool]:
    """MLflow parameterへ保存できるscalar mappingへ全formal設定を展開する."""

    values: dict[str, str | int | float | bool] = {}

    def visit(prefix: str, value: object) -> None:
        if isinstance(value, Mapping):
            for key in sorted(value):
                visit(f"{prefix}.{key}" if prefix else str(key), value[key])
            return
        if isinstance(value, (list, tuple)):
            values[prefix] = json.dumps(value, ensure_ascii=True, sort_keys=True)
            return
        if value is None:
            values[prefix] = "null"
            return
        if isinstance(value, (str, int, float, bool)):
            values[prefix] = value
            return
        values[prefix] = str(value)

    visit(
        "",
        {
            "data": config.data.to_dict(),
            "model": config.model.to_dict(),
            "preprocess": preprocess_schema(config.data.constraints),
            "trainer": config.trainer.to_dict(),
            "dependencies": dependency_versions(),
            "optimizer": {"name": "AdamW"},
            "scheduler": {"name": "ReduceLROnPlateau"},
            "run_kind": config.run_kind,
        },
    )
    return cast(
        dict[str, str | int | float | bool],
        _sanitize_persisted_value(values),
    )


def _absolute_path(value: str | None, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(cast(str, value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _strict_kwargs(
    raw: Mapping[str, object], allowed: set[str], label: str
) -> dict[str, Any]:
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown {label} config keys: {sorted(unknown)}")
    return {key: value for key, value in raw.items()}


def train_config_from_mapping(
    raw: Mapping[str, object],
    *,
    base_directory: Path,
) -> tuple[TrainConfig, LoggerConfig]:
    """Hydra解決済みmappingを未知key拒否でfrozen configへ変換する."""

    allowed = {
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
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown train config keys: {sorted(unknown)}")
    data_raw = cast(Mapping[str, object], raw["data"])
    data_allowed = {
        "manifest",
        "roots",
        "split_manifest",
        "split_seed",
        "train_ratio",
        "validation_ratio",
        "test_ratio",
        "constraints",
        "augmentation",
        "max_batch_pixels",
        "max_batch_size",
    }
    if unknown_data := set(data_raw) - data_allowed:
        raise ValueError(f"unknown data config keys: {sorted(unknown_data)}")
    constraints_raw = cast(Mapping[str, object], data_raw["constraints"])
    augmentation_raw = cast(Mapping[str, object], data_raw["augmentation"])
    roots_raw = cast(Sequence[str], data_raw.get("roots", ()))
    data = DataConfig(
        manifest=_absolute_path(
            cast(str | None, data_raw.get("manifest")), base_directory
        ),
        roots=tuple(
            cast(Path, _absolute_path(value, base_directory)) for value in roots_raw
        ),
        split_manifest=_absolute_path(
            cast(str | None, data_raw.get("split_manifest")), base_directory
        ),
        split_seed=int(cast(Any, data_raw.get("split_seed", 42))),
        train_ratio=float(cast(Any, data_raw.get("train_ratio", 0.70))),
        validation_ratio=float(cast(Any, data_raw.get("validation_ratio", 0.15))),
        test_ratio=float(cast(Any, data_raw.get("test_ratio", 0.15))),
        constraints=ImageConstraints(
            **_strict_kwargs(
                constraints_raw,
                {
                    "min_size",
                    "max_size",
                    "max_pixels",
                    "stride",
                    "normalization_epsilon",
                },
                "image constraints",
            )
        ),
        augmentation=AugmentationConfig(
            **_strict_kwargs(
                augmentation_raw,
                {"enabled", "min_scale", "max_scale"},
                "augmentation",
            )
        ),
        max_batch_pixels=int(cast(Any, data_raw.get("max_batch_pixels", 8_388_608))),
        max_batch_size=int(cast(Any, data_raw.get("max_batch_size", 32))),
    )
    checkpoint_raw = cast(Mapping[str, object], raw["checkpoint"])
    _strict_kwargs(
        checkpoint_raw,
        {
            "directory",
            "resume_checkpoint",
            "initial_weights",
            "save_interval_steps",
            "save_interval_seconds",
        },
        "checkpoint",
    )
    checkpoint = CheckpointConfig(
        directory=cast(
            Path,
            _absolute_path(cast(str, checkpoint_raw["directory"]), base_directory),
        ),
        resume_checkpoint=_absolute_path(
            cast(str | None, checkpoint_raw.get("resume_checkpoint")), base_directory
        ),
        initial_weights=_absolute_path(
            cast(str | None, checkpoint_raw.get("initial_weights")), base_directory
        ),
        save_interval_steps=int(
            cast(Any, checkpoint_raw.get("save_interval_steps", 500))
        ),
        save_interval_seconds=float(
            cast(Any, checkpoint_raw.get("save_interval_seconds", 300.0))
        ),
    )
    logger_raw = cast(Mapping[str, object], raw["logger"])
    _strict_kwargs(
        logger_raw,
        {"tracking_uri", "experiment_name", "run_name", "metric_retry_count"},
        "logger",
    )
    logger_config = LoggerConfig(
        tracking_uri=str(logger_raw["tracking_uri"]),
        experiment_name=str(logger_raw["experiment_name"]),
        run_name=cast(str | None, logger_raw.get("run_name")),
        metric_retry_count=int(cast(Any, logger_raw.get("metric_retry_count", 3))),
    )
    model_raw = cast(Mapping[str, object], raw["model"])
    model_kwargs = _strict_kwargs(
        model_raw,
        {
            "family",
            "input_channels",
            "stem_channels",
            "stage_channels",
            "blocks_per_stage",
            "group_norm_groups",
            "hidden_features",
            "log_variance_min",
            "log_variance_max",
        },
        "model",
    )
    for tuple_key in ("stem_channels", "stage_channels", "blocks_per_stage"):
        if tuple_key in model_kwargs:
            model_kwargs[tuple_key] = tuple(model_kwargs[tuple_key])
    trainer_raw = cast(Mapping[str, object], raw["trainer"])
    hpo_raw = raw.get("hpo")
    hpo_config = None
    if isinstance(hpo_raw, Mapping) and hpo_raw.get("trial_number") is not None:
        hpo_config = HpoTrialConfig(
            **_strict_kwargs(
                cast(Mapping[str, object], hpo_raw),
                {
                    "study_name",
                    "trial_number",
                    "search_config_fingerprint",
                    "storage_uri_redacted",
                },
                "hpo",
            )
        )
    config = TrainConfig(
        data=data,
        checkpoint=checkpoint,
        model=PasteVolumeModelConfig(**model_kwargs),
        trainer=TrainerConfig(
            **_strict_kwargs(
                trainer_raw,
                {
                    "device",
                    "seed",
                    "learning_rate",
                    "weight_decay",
                    "gradient_accumulation_steps",
                    "gradient_clip_norm",
                    "max_epochs",
                    "max_steps",
                    "early_stopping_patience",
                    "early_stopping_min_delta",
                    "scheduler_factor",
                    "scheduler_patience",
                    "compile_enabled",
                    "compile_backend",
                    "compile_mode",
                    "deterministic",
                    "deadline_seconds",
                    "finalization_grace_seconds",
                    "max_train_samples",
                    "fine_tune_full_model",
                },
                "trainer",
            )
        ),
        run_kind=cast(Literal["base-train", "finetune"], raw["run_kind"]),
        repository_root=cast(
            Path,
            _absolute_path(
                cast(str, raw.get("repository_root", str(base_directory))),
                base_directory,
            ),
        ),
        parent_base_run_id=cast(str | None, raw.get("parent_base_run_id")),
        hpo=hpo_config,
    )
    return config, logger_config


def hydra_train(raw_config: object) -> TrainResult:
    """DictConfigを境界で解決・検証してformal MLflow runを開始する."""

    from hydra.core.hydra_config import HydraConfig
    from omegaconf import OmegaConf

    resolved = OmegaConf.to_container(raw_config, resolve=True, throw_on_missing=True)
    if not isinstance(resolved, dict):
        raise ValueError("resolved Hydra train config must be a mapping")
    base = (
        Path(HydraConfig.get().runtime.cwd) if HydraConfig.initialized() else Path.cwd()
    )
    config, logger_config = train_config_from_mapping(
        cast(Mapping[str, object], resolved), base_directory=base
    )
    persisted_resolved = cast(dict[str, object], _sanitize_persisted_value(resolved))
    persisted_overrides = (
        tuple(
            _sanitize_persisted_text(item) for item in HydraConfig.get().overrides.task
        )
        if HydraConfig.initialized()
        else ()
    )
    config = replace(
        config,
        hydra_resolved_yaml=OmegaConf.to_yaml(
            OmegaConf.create(persisted_resolved), resolve=True
        ),
        hydra_overrides=persisted_overrides,
    )
    resume_run_id: str | None = None
    if config.checkpoint.resume_checkpoint is not None:
        from .training import load_training_checkpoint

        resume_run_id = cast(
            str,
            load_training_checkpoint(config.checkpoint.resume_checkpoint)["run_id"],
        )
    logger = MLflowExperimentLogger(
        tracking_uri=logger_config.tracking_uri,
        experiment_name=logger_config.experiment_name,
        resume_run_id=resume_run_id,
        run_name=logger_config.run_name,
        metric_retry_count=logger_config.metric_retry_count,
    )
    return train(
        config,
        logger,
        formal_tracking_uri=logger_config.tracking_uri,
    )


def main() -> None:
    """`python -m pcbasm.pasting.paste_volume.train` entrypoint."""

    import hydra

    @hydra.main(version_base="1.3", config_path="conf", config_name="train")
    def run(config: object) -> float:
        result = hydra_train(config)
        print(
            json.dumps({"run_id": result.run_id, "weights": str(result.weights_path)})
        )
        return result.best_validation_metrics.gaussian_nll

    run()


if __name__ == "__main__":
    main()


__all__ = [
    "CheckpointConfig",
    "DataConfig",
    "HpoTrialConfig",
    "LoggerConfig",
    "PreparedTrainingData",
    "TrainConfig",
    "TrainResult",
    "TrainerConfig",
    "hydra_train",
    "prepare_training_data",
    "preprocess_schema",
    "sanitize_persisted_uri",
    "sanitize_persisted_text",
    "sanitize_persisted_value",
    "summarize_persisted_git_diff",
    "train",
    "train_config_from_mapping",
    "training_protocol_fingerprint",
]
