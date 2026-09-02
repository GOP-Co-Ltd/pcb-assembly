"""Paste-volume study identity adapter for the generic Hydra sweeper."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omegaconf import DictConfig, OmegaConf

_OPTIMIZATION_RESULTS_KIND = "pcbasm-paste-volume-optuna-results"

from ml.tuning.hydra_sweeper import (
    PersistentOptunaSweeper as _GenericPersistentOptunaSweeper,
    StudyIdentity,
)


def _study_identity(config: DictConfig) -> StudyIdentity:
    from .data import resolve_dataset_inputs

    base_directory = Path(str(config.hydra.runtime.cwd)).resolve()
    manifest_value = OmegaConf.select(config, "data.manifest")
    roots_value = OmegaConf.select(config, "data.roots", default=[])
    manifest = None
    if manifest_value not in (None, "???"):
        candidate = Path(str(manifest_value)).expanduser()
        manifest = (
            candidate.resolve()
            if candidate.is_absolute()
            else (base_directory / candidate).resolve()
        )
    roots: list[Path] = []
    for value in roots_value or []:
        candidate = Path(str(value)).expanduser()
        roots.append(
            candidate.resolve()
            if candidate.is_absolute()
            else (base_directory / candidate).resolve()
        )
    composite = resolve_dataset_inputs(manifest=manifest, roots=roots)
    return StudyIdentity(
        model_family=str(config.model.family),
        dataset_fingerprint=composite.composite_fingerprint,
    )


class PersistentOptunaSweeper(_GenericPersistentOptunaSweeper):
    """Hydra-instantiated paste-volume sweeper delegate."""

    def __init__(
        self,
        sampler: Any,
        direction: Any,
        storage: str,
        study_name: str | None,
        n_trials: int,
        n_jobs: int,
        max_failure_rate: float,
        custom_search_space: str | None,
        params: DictConfig | None,
    ) -> None:
        super().__init__(
            sampler,
            direction,
            storage,
            study_name,
            n_trials,
            n_jobs,
            max_failure_rate,
            custom_search_space,
            params,
            study_identity_resolver=_study_identity,
            result_kind=_OPTIMIZATION_RESULTS_KIND,
        )


__all__ = ["PersistentOptunaSweeper"]
