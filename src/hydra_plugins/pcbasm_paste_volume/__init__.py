"""Hydra plugin namespace for the paste-volume Optuna sweeper.

Hydra scans this namespace during normal startup. The actual Optuna
sweeper is therefore imported only when Hydra instantiates this
lightweight delegate.
"""

from __future__ import annotations

from typing import Any, override

from hydra.plugins.sweeper import Sweeper
from hydra.types import HydraContext, TaskFunction
from omegaconf import DictConfig


class PersistentOptunaSweeper(Sweeper):
    """Lazily delegate to the project-specific persistent Optuna sweeper."""

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
        from pcbasm.pasting.paste_volume.hpo_sweeper import (
            PersistentOptunaSweeper as Implementation,
        )

        # Preserve the programmatic API contract after explicit instantiation.
        Implementation.register(type(self))
        self._implementation: Sweeper = Implementation(
            sampler,
            direction,
            storage,
            study_name,
            n_trials,
            n_jobs,
            max_failure_rate,
            custom_search_space,
            params,
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
    def sweep(self, arguments: list[str]) -> Any:
        return self._implementation.sweep(arguments)


__all__ = ["PersistentOptunaSweeper"]
