"""Reusable persistent Optuna sweeper with explicit MLflow lineage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

import optuna
from hydra.plugins.sweeper import Sweeper
from hydra.types import HydraContext, TaskFunction
from hydra.utils import instantiate
from omegaconf import DictConfig, OmegaConf

from hydra_plugins.hydra_optuna_sweeper._impl import OptunaSweeperImpl

from .optuna import (
    build_optuna_study_name,
    build_trial_metadata_overrides,
    optuna_trials_payload,
    search_config_fingerprint,
    validate_optuna_storage,
    write_optimization_results,
)


@dataclass(frozen=True)
class StudyIdentity:
    model_family: str
    dataset_fingerprint: str


StudyIdentityResolver = Callable[[DictConfig], StudyIdentity]


class _PersistentOptunaSweeperImpl(OptunaSweeperImpl):
    def __init__(
        self,
        *args: Any,
        study_identity_resolver: StudyIdentityResolver,
        result_kind: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._study_identity_resolver = study_identity_resolver
        self._result_kind = result_kind
        self._search_fingerprint = ""
        self._target_trial_count = self.n_trials

    @override
    def setup(
        self,
        *,
        hydra_context: HydraContext,
        task_function: TaskFunction,
        config: DictConfig,
    ) -> None:
        super().setup(
            hydra_context=hydra_context,
            task_function=task_function,
            config=config,
        )
        if not isinstance(self.storage, str):
            raise ValueError("persistent Optuna storage URI is required")
        validate_optuna_storage(self.storage)
        search_config_raw = OmegaConf.to_container(
            config.hydra.sweeper, resolve=True, throw_on_missing=True
        )
        if not isinstance(search_config_raw, dict):
            raise ValueError("Hydra sweeper config must be a mapping")
        search_config = {
            "sampler": search_config_raw.get("sampler"),
            "direction": search_config_raw.get("direction"),
            "params": search_config_raw.get("params"),
            "n_jobs": search_config_raw.get("n_jobs"),
            "max_failure_rate": search_config_raw.get("max_failure_rate"),
        }
        self._search_fingerprint = search_config_fingerprint(search_config)
        identity = self._study_identity_resolver(config)
        expected_name = build_optuna_study_name(
            model_family=identity.model_family,
            dataset_fingerprint=identity.dataset_fingerprint,
            search_config=search_config,
        )
        if self.study_name not in (None, "", "auto", expected_name):
            raise ValueError(
                "configured Optuna study_name does not match dataset/search identity"
            )
        self.study_name = expected_name

    @override
    def _configure_trials(
        self,
        trials: list[optuna.trial.Trial],
        search_space_distributions: dict[str, optuna.distributions.BaseDistribution],
        fixed_params: dict[str, Any],
        fixed_overrides: list[str],
    ) -> tuple[tuple[str, ...], ...]:
        configured = super()._configure_trials(
            trials,
            search_space_distributions,
            fixed_params,
            fixed_overrides,
        )
        if self.config is None or self.study_name is None:
            raise RuntimeError("Optuna sweeper has not been set up")
        sweep_directory = Path(str(self.config.hydra.sweep.dir))
        if not sweep_directory.is_absolute():
            sweep_directory = Path(str(self.config.hydra.runtime.cwd)) / sweep_directory
        result: list[tuple[str, ...]] = []
        for trial, overrides in zip(trials, configured, strict=True):
            trial_directory = (sweep_directory / f"trial-{trial.number}").resolve()
            metadata = build_trial_metadata_overrides(
                study_name=self.study_name,
                trial_number=trial.number,
                search_fingerprint=self._search_fingerprint,
                storage_uri=str(self.storage),
                checkpoint_directory=trial_directory,
            )
            result.append((*overrides, *metadata))
        return tuple(result)

    def _study(self) -> optuna.study.Study:
        if self.study_name is None:
            raise RuntimeError("Optuna study name is not initialized")
        directions = self._get_directions()
        return optuna.create_study(
            study_name=self.study_name,
            storage=self.storage,
            sampler=self.sampler,
            directions=directions,
            load_if_exists=True,
        )

    def _mlflow_run_ids(self) -> dict[int, str]:
        if self.config is None or self.study_name is None:
            raise RuntimeError("Optuna sweeper has not been set up")
        import mlflow
        from mlflow import MlflowClient

        mlflow.set_tracking_uri(str(self.config.logger.tracking_uri))
        client = MlflowClient()
        experiment = client.get_experiment_by_name(
            str(self.config.logger.experiment_name)
        )
        if experiment is None:
            raise RuntimeError("HPO MLflow experiment does not exist")
        runs = client.search_runs(
            [experiment.experiment_id],
            filter_string=f"tags.`hpo.study_name` = '{self.study_name}'",
            max_results=50_000,
        )
        result: dict[int, str] = {}
        for run in runs:
            value = run.data.tags.get("hpo.trial_number")
            if value is not None:
                result[int(value)] = str(run.info.run_id)
        return result

    def _write_complete_results(self, study: optuna.study.Study) -> Path:
        if self.config is None or self.study_name is None:
            raise RuntimeError("Optuna sweeper has not been set up")
        path = Path(str(self.config.hydra.sweep.dir)) / "optimization_results.yaml"
        run_ids = self._mlflow_run_ids()
        completed_numbers = {
            int(trial.number)
            for trial in study.get_trials(deepcopy=False)
            if trial.state == optuna.trial.TrialState.COMPLETE
        }
        missing_run_ids = completed_numbers - set(run_ids)
        if missing_run_ids:
            raise RuntimeError(
                "completed Optuna trials are missing MLflow run lineage: "
                f"{sorted(missing_run_ids)}"
            )
        result_path = write_optimization_results(
            path,
            kind=self._result_kind,
            study_name=self.study_name,
            storage_uri=str(self.storage),
            direction=str(self.direction),
            trials=optuna_trials_payload(study, run_ids_by_trial=run_ids),
        )
        from mlflow import MlflowClient

        client = MlflowClient(tracking_uri=str(self.config.logger.tracking_uri))
        for run_id in run_ids.values():
            client.log_artifact(run_id, str(result_path), artifact_path="hpo")
        return result_path

    @override
    def sweep(self, arguments: list[str]) -> None:
        study = self._study()
        completed = sum(
            trial.state == optuna.trial.TrialState.COMPLETE
            for trial in study.get_trials(deepcopy=False)
        )
        remaining = max(0, self._target_trial_count - completed)
        original = self.n_trials
        self.n_trials = remaining
        try:
            if remaining:
                super().sweep(arguments)
            study = self._study()
            self._write_complete_results(study)
        finally:
            self.n_trials = original


class PersistentOptunaSweeper(Sweeper):
    """Hydra sweeper with target-specific study identity injection."""

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
        *,
        study_identity_resolver: StudyIdentityResolver,
        result_kind: str,
    ) -> None:
        sampler_instance = (
            sampler
            if isinstance(sampler, optuna.samplers.BaseSampler)
            else instantiate(sampler)
        )
        self._implementation = _PersistentOptunaSweeperImpl(
            sampler_instance,
            direction,
            storage,
            study_name,
            n_trials,
            n_jobs,
            max_failure_rate,
            custom_search_space,
            params,
            study_identity_resolver=study_identity_resolver,
            result_kind=result_kind,
        )

    @override
    def setup(
        self,
        *,
        hydra_context: HydraContext,
        task_function: TaskFunction,
        config: DictConfig,
    ) -> None:
        self._implementation.setup(
            hydra_context=hydra_context,
            task_function=task_function,
            config=config,
        )

    @override
    def sweep(self, arguments: list[str]) -> None:
        self._implementation.sweep(arguments)
