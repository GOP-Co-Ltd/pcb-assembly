from __future__ import annotations

import copy
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from urllib.request import urlopen

import numpy as np
import pytest
import torch
from torch import Tensor

from ml.artifacts.formal import formal_artifact_attestation_path
from ml.cli.provenance import (
    sanitize_persisted_text,
    sanitize_persisted_uri,
    summarize_persisted_git_diff,
)
from ml.data.image import AugmentationConfig, ImageConstraints
from ml.paste_volume.metrics import (
    EvaluationPredictions,
    evaluate_batches,
    fit_log_variance_offset,
)
from ml.paste_volume.model import (
    PasteVolumeModelConfig,
    PasteVolumeResNet,
)
from ml.paste_volume.train import (
    DataConfig,
    HpoTrialConfig,
    LoggerConfig,
    TrainConfig,
    hydra_train,
    prepare_training_data,
    preprocess_schema,
    train,
    train_config_from_mapping,
    training_protocol_fingerprint,
)
from ml.paste_volume.training import (
    CheckpointConfig,
    TrainerConfig,
    TrainingCoreConfig,
    TrainingDeadlineExceeded,
    TrainingInterrupted,
    train_model,
)
from ml.paste_volume.training_artifacts import (
    CHECKPOINT_KIND,
    CHECKPOINT_SCHEMA_VERSION,
    atomic_torch_save,
    extract_best_weights,
    load_formal_training_weights,
    load_training_checkpoint,
)
from ml.paste_volume.training_types import TrainingBatch
from ml.training.experiment import MLflowExperimentLogger, NullExperimentLogger
from ml.training.random_state import seed_everything
from tests.ml.paste_volume.support_data import write_synthetic_session

_IMAGE_SHAPE = (6, 32, 32)


def _tiny_model_config() -> PasteVolumeModelConfig:
    return PasteVolumeModelConfig(
        stem_channels=(4, 4, 4),
        stage_channels=(4, 4, 4),
        blocks_per_stage=(1, 1, 1),
        group_norm_groups=1,
        hidden_features=4,
    )


def _images_for(sample_ids: tuple[str, ...]) -> Tensor:
    ramp = torch.linspace(-1.0, 1.0, math.prod(_IMAGE_SHAPE)).reshape(_IMAGE_SHAPE)
    return torch.stack(
        [
            ramp + (sum(sample_id.encode("utf-8")) % 17) / 100.0
            for sample_id in sample_ids
        ]
    )


def _training_batch(
    sample_ids: tuple[str, ...],
    targets: Mapping[str, float],
    weights: Mapping[str, float] | None = None,
    *,
    target_dtype: torch.dtype = torch.float32,
) -> TrainingBatch:
    batch_size = len(sample_ids)
    return TrainingBatch(
        image_6ch=_images_for(sample_ids),
        valid_pixel_mask=torch.ones((batch_size, 1, 32, 32), dtype=torch.bool),
        pixel_per_mm=torch.full((batch_size, 1), 20.0),
        target_volume_ul=torch.tensor(
            [[targets[sample_id]] for sample_id in sample_ids], dtype=target_dtype
        ),
        sample_weight=torch.tensor(
            [
                [1.0 if weights is None else weights[sample_id]]
                for sample_id in sample_ids
            ]
        ),
        sample_ids=sample_ids,
    )


class _TensorTrainingData:
    def __init__(
        self,
        *,
        train_targets: Mapping[str, float] | None = None,
        validation_targets: Mapping[str, float] | None = None,
        train_plan: tuple[tuple[str, ...], ...] | None = None,
        fail_on_training_call: int | None = None,
        signal_on_training_call: int | None = None,
        termination_signal: signal.Signals = signal.SIGTERM,
        signal_on_evaluation_call: int | None = None,
        evaluation_plan: tuple[tuple[str, ...], ...] | None = None,
        training_batch_delay_seconds: float = 0.0,
        nonfinite_on_training_call: tuple[int, Literal["loss", "gradient"]]
        | None = None,
    ) -> None:
        self._train_targets = dict(
            train_targets
            or {
                "train-0": 1.5,
                "train-1": 1.75,
                "train-2": 2.0,
            }
        )
        self._validation_targets = dict(
            validation_targets or {"validation-0": 1.6, "validation-1": 1.9}
        )
        self._train_plan = train_plan or tuple(
            (sample_id,) for sample_id in self._train_targets
        )
        self._fail_on_training_call = fail_on_training_call
        self._signal_on_training_call = signal_on_training_call
        self._termination_signal = termination_signal
        self._signal_on_evaluation_call = signal_on_evaluation_call
        self._evaluation_plan = evaluation_plan
        self._training_batch_delay_seconds = training_batch_delay_seconds
        self._nonfinite_on_training_call = nonfinite_on_training_call
        self._training_call_count = 0
        self._evaluation_call_count = 0
        self.training_requests: list[tuple[int, tuple[str, ...]]] = []

    @property
    def train_sample_ids(self) -> tuple[str, ...]:
        return tuple(sample_id for batch in self._train_plan for sample_id in batch)

    @property
    def training_call_count(self) -> int:
        return self._training_call_count

    @property
    def evaluation_call_count(self) -> int:
        return self._evaluation_call_count

    def training_batch_plan(self, epoch: int) -> tuple[tuple[str, ...], ...]:
        return self._train_plan

    def training_batch(
        self, sample_ids: tuple[str, ...], *, epoch: int
    ) -> TrainingBatch:
        self._training_call_count += 1
        self.training_requests.append((epoch, sample_ids))
        if self._training_batch_delay_seconds:
            time.sleep(self._training_batch_delay_seconds)
        if self._training_call_count == self._fail_on_training_call:
            raise RuntimeError("controlled interruption")
        if self._training_call_count == self._signal_on_training_call:
            os.kill(os.getpid(), self._termination_signal)
        batch = _training_batch(sample_ids, self._train_targets)
        if (
            self._nonfinite_on_training_call is not None
            and self._training_call_count == self._nonfinite_on_training_call[0]
        ):
            failure_kind = self._nonfinite_on_training_call[1]
            target = batch.target_volume_ul.clone()
            if failure_kind == "loss":
                target[0, 0] = float("nan")
            else:
                target = target.to(torch.float64)
                target[0, 0] = 1e100
            batch = replace(batch, target_volume_ul=target)
        return batch

    def evaluation_batch_plan(
        self, split: Literal["validation", "test"]
    ) -> tuple[tuple[str, ...], ...]:
        return self._evaluation_plan or (tuple(self._validation_targets),)

    def evaluation_batch(
        self,
        sample_ids: tuple[str, ...],
        *,
        split: Literal["validation", "test"],
    ) -> TrainingBatch:
        self._evaluation_call_count += 1
        if self._evaluation_call_count == self._signal_on_evaluation_call:
            os.kill(os.getpid(), self._termination_signal)
        return _training_batch(sample_ids, self._validation_targets)


class _EncodedPredictionData:
    def __init__(
        self,
        *,
        means: tuple[float, ...],
        log_variances: tuple[float, ...],
        targets: tuple[float, ...],
        weights: tuple[float, ...],
    ) -> None:
        self._sample_ids = tuple(f"sample-{index}" for index in range(len(means)))
        self._means = means
        self._log_variances = log_variances
        self._targets = targets
        self._weights = weights

    def training_batch_plan(self, epoch: int) -> tuple[tuple[str, ...], ...]:
        return (self._sample_ids,)

    def training_batch(
        self, sample_ids: tuple[str, ...], *, epoch: int
    ) -> TrainingBatch:
        return self.evaluation_batch(sample_ids, split="validation")

    def evaluation_batch_plan(
        self, split: Literal["validation", "test"]
    ) -> tuple[tuple[str, ...], ...]:
        return (self._sample_ids,)

    def evaluation_batch(
        self,
        sample_ids: tuple[str, ...],
        *,
        split: Literal["validation", "test"],
    ) -> TrainingBatch:
        indices = [self._sample_ids.index(sample_id) for sample_id in sample_ids]
        image = torch.zeros((len(indices), *_IMAGE_SHAPE))
        for row, index in enumerate(indices):
            image[row, 0, 0, 0] = self._means[index]
            image[row, 1, 0, 0] = self._log_variances[index]
        return TrainingBatch(
            image_6ch=image,
            valid_pixel_mask=torch.ones((len(indices), 1, 32, 32), dtype=torch.bool),
            pixel_per_mm=torch.full((len(indices), 1), 20.0),
            target_volume_ul=torch.tensor(
                [[self._targets[index]] for index in indices]
            ),
            sample_weight=torch.tensor([[self._weights[index]] for index in indices]),
            sample_ids=sample_ids,
        )


def _decode_prediction(
    image_6ch: Tensor,
    valid_pixel_mask: Tensor,
    pixel_per_mm: Tensor,
) -> tuple[Tensor, Tensor]:
    del valid_pixel_mask, pixel_per_mm
    return (
        image_6ch[:, 0, 0, 0].reshape(-1, 1),
        image_6ch[:, 1, 0, 0].reshape(-1, 1),
    )


def _core_config(
    directory: Path,
    data: _TensorTrainingData,
    *,
    run_kind: Literal["base-train", "finetune"] = "base-train",
    resume_checkpoint: Path | None = None,
    initial_weights: Path | None = None,
    parent_base_run_id: str | None = None,
    max_epochs: int = 1,
    max_steps: int | None = None,
    gradient_accumulation_steps: int = 1,
    early_stopping_min_delta: float = 0.0,
    deadline_seconds: float | None = None,
    finalization_grace_seconds: float = 300.0,
    fine_tune_full_model: bool = False,
    compile_enabled: bool = False,
    compile_backend: str = "inductor",
    dataset_fingerprint: str = "sha256:" + "d" * 64,
    config_fingerprint: str = "sha256:" + "c" * 64,
    training_protocol_fingerprint: str = "sha256:" + "f" * 64,
) -> TrainingCoreConfig:
    return TrainingCoreConfig(
        run_kind=run_kind,
        model=_tiny_model_config(),
        trainer=TrainerConfig(
            device="cpu",
            seed=19,
            learning_rate=1e-2,
            weight_decay=0.0,
            gradient_accumulation_steps=gradient_accumulation_steps,
            max_epochs=max_epochs,
            max_steps=max_steps,
            early_stopping_patience=max_epochs + 2,
            early_stopping_min_delta=early_stopping_min_delta,
            scheduler_patience=max_epochs + 2,
            compile_enabled=compile_enabled,
            compile_backend=compile_backend,
            deterministic=True,
            deadline_seconds=deadline_seconds,
            finalization_grace_seconds=finalization_grace_seconds,
            fine_tune_full_model=fine_tune_full_model,
        ),
        checkpoint=CheckpointConfig(
            directory=directory,
            resume_checkpoint=resume_checkpoint,
            initial_weights=initial_weights,
            save_interval_steps=1,
            save_interval_seconds=3600.0,
        ),
        dataset_fingerprint=dataset_fingerprint,
        split_fingerprint="sha256:" + "e" * 64,
        config_fingerprint=config_fingerprint,
        training_protocol_fingerprint=training_protocol_fingerprint,
        preprocess_schema={"schema_version": 1, "normalization": "sample"},
        train_sample_ids=data.train_sample_ids,
        parent_base_run_id=parent_base_run_id,
    )


def _formal_logger(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    experiment_name: str,
) -> tuple[str, MLflowExperimentLogger]:
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    tracking_uri = (directory / "mlruns").resolve().as_uri()
    return tracking_uri, MLflowExperimentLogger(
        tracking_uri=tracking_uri,
        experiment_name=experiment_name,
    )


class _MLflowServer:
    def __init__(self, root: Path) -> None:
        self._root = root
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            self._port = int(server.getsockname()[1])
        self.uri = f"http://127.0.0.1:{self._port}"
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self._root.mkdir(parents=True)
        self._process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "mlflow",
                "server",
                "--backend-store-uri",
                f"sqlite:///{self._root / 'mlflow.db'}",
                "--default-artifact-root",
                (self._root / "artifacts").as_uri(),
                "--host",
                "127.0.0.1",
                "--port",
                str(self._port),
                "--workers",
                "1",
            ),
            cwd=self._root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("MLflow server exited during startup")
            try:
                with urlopen(self.uri + "/health", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("MLflow server did not become ready")

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        self._process.wait(timeout=10)
        self._process = None


def _assert_nested_equal(actual: object, expected: object) -> None:
    if isinstance(actual, Tensor) and isinstance(expected, Tensor):
        torch.testing.assert_close(actual, expected, rtol=0.0, atol=0.0)
        return
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        assert set(actual) == set(expected)
        for key in actual:
            _assert_nested_equal(actual[key], expected[key])
        return
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        assert len(actual) == len(expected)
        for actual_item, expected_item in zip(actual, expected, strict=True):
            _assert_nested_equal(actual_item, expected_item)
        return
    if isinstance(actual, np.ndarray) and isinstance(expected, np.ndarray):
        np.testing.assert_array_equal(actual, expected)
        return
    assert actual == expected


def _raw_train_mapping(tmp_path: Path) -> dict[str, object]:
    return {
        "data": {
            "manifest": str(tmp_path / "dataset.json"),
            "roots": [],
            "split_manifest": None,
            "split_seed": 42,
            "train_ratio": 0.7,
            "validation_ratio": 0.15,
            "test_ratio": 0.15,
            "constraints": {
                "min_size": 32,
                "max_size": 1024,
                "max_pixels": 262_144,
                "stride": 32,
                "normalization_epsilon": 1e-5,
            },
            "augmentation": {
                "enabled": False,
                "min_scale": 0.8,
                "max_scale": 1.2,
            },
            "max_batch_pixels": 262_144,
            "max_batch_size": 4,
        },
        "model": _tiny_model_config().to_dict(),
        "trainer": {
            "device": "cpu",
            "seed": 19,
            "learning_rate": 1e-3,
            "weight_decay": 0.0,
            "gradient_accumulation_steps": 1,
            "gradient_clip_norm": 1.0,
            "max_epochs": 1,
            "max_steps": None,
            "early_stopping_patience": 2,
            "early_stopping_min_delta": 0.0,
            "scheduler_factor": 0.5,
            "scheduler_patience": 2,
            "compile_enabled": False,
            "compile_backend": "inductor",
            "compile_mode": "default",
            "deterministic": True,
            "deadline_seconds": None,
            "finalization_grace_seconds": 300.0,
            "max_train_samples": None,
            "fine_tune_full_model": False,
        },
        "logger": {
            "tracking_uri": "http://127.0.0.1:5000",
            "experiment_name": "test",
            "run_name": None,
            "metric_retry_count": 1,
        },
        "checkpoint": {
            "directory": str(tmp_path / "run"),
            "resume_checkpoint": None,
            "initial_weights": None,
            "save_interval_steps": 1,
            "save_interval_seconds": 60.0,
        },
        "run_kind": "base-train",
        "repository_root": str(tmp_path),
        "parent_base_run_id": None,
    }


class TestRegressionMetricsAndCalibration:
    def test_evaluation_uses_sample_weights_for_physical_metrics(self):
        data = _EncodedPredictionData(
            means=(1.0, 4.0),
            log_variances=(0.0, 0.0),
            targets=(2.0, 2.0),
            weights=(1.0, 3.0),
        )

        predictions = evaluate_batches(
            _decode_prediction,
            data,
            split="validation",
            device=torch.device("cpu"),
        )

        expected_nll = (0.5 + 3.0 * 2.0) / 4.0
        assert predictions.sample_ids == ("sample-0", "sample-1")
        assert predictions.metrics.gaussian_nll == pytest.approx(expected_nll)
        assert predictions.metrics.mae_ul == pytest.approx(1.75)
        assert predictions.metrics.rmse_ul == pytest.approx(math.sqrt(3.25))
        assert predictions.metrics.normalized_error_mean == pytest.approx(0.625)
        assert predictions.metrics.normalized_error_std == pytest.approx(
            math.sqrt(0.421875)
        )
        assert predictions.metrics.one_std_coverage == pytest.approx(0.25)
        assert predictions.metrics.mean_prediction_std_ul == pytest.approx(1.0)
        assert predictions.metrics.sample_count == 2

    def test_log_variance_calibration_is_fit_only_from_weighted_predictions(self):
        data = _EncodedPredictionData(
            means=(1.0, 4.0),
            log_variances=(0.0, 0.0),
            targets=(2.0, 2.0),
            weights=(1.0, 3.0),
        )
        uncalibrated = evaluate_batches(
            _decode_prediction,
            data,
            split="validation",
            device=torch.device("cpu"),
        )

        offset = fit_log_variance_offset(uncalibrated)
        calibrated = evaluate_batches(
            _decode_prediction,
            data,
            split="validation",
            device=torch.device("cpu"),
            log_variance_offset=offset,
        )

        assert offset == pytest.approx(math.log(3.25))
        assert calibrated.metrics.gaussian_nll < uncalibrated.metrics.gaussian_nll
        torch.testing.assert_close(
            calibrated.mean_volume_ul, uncalibrated.mean_volume_ul
        )

    def test_uncertainty_calibration_rejects_zero_residual_predictions(self):
        predictions = EvaluationPredictions(
            metrics=evaluate_batches(
                _decode_prediction,
                _EncodedPredictionData(
                    means=(1.0,),
                    log_variances=(0.0,),
                    targets=(1.0,),
                    weights=(1.0,),
                ),
                split="validation",
                device=torch.device("cpu"),
            ).metrics,
            mean_volume_ul=torch.tensor([[1.0]]),
            log_variance_volume_ul2=torch.tensor([[0.0]]),
            target_volume_ul=torch.tensor([[1.0]]),
            sample_weight=torch.tensor([[1.0]]),
            sample_ids=("sample-0",),
        )

        with pytest.raises(ValueError) as error:
            fit_log_variance_offset(predictions)

        assert "cannot calibrate uncertainty" in str(error.value)


class TestCheckpointRolesAndSelection:
    def test_normal_training_publishes_atomic_latest_best_final_and_weights(
        self, tmp_path: Path
    ):
        data = _TensorTrainingData()
        config = _core_config(tmp_path / "run", data)
        logger = NullExperimentLogger(run_id="role-test-run")

        result = train_model(config, data, logger)

        latest = load_training_checkpoint(result.latest_checkpoint)
        best = load_training_checkpoint(result.best_checkpoint)
        final = load_training_checkpoint(result.final_checkpoint)
        weights = cast(
            dict[str, object],
            torch.load(result.weights_path, map_location="cpu", weights_only=True),
        )
        assert latest["checkpoint_role"] == "latest"
        assert best["checkpoint_role"] == "best"
        assert final["checkpoint_role"] == "final"
        assert latest["run_id"] == best["run_id"] == final["run_id"] == result.run_id
        assert latest["uncertainty_log_variance_offset"] is None
        assert best["uncertainty_log_variance_offset"] == pytest.approx(
            result.log_variance_offset
        )
        assert final["uncertainty_log_variance_offset"] == pytest.approx(
            result.log_variance_offset
        )
        assert best["resolved_config"] == {}
        assert weights["source_run_id"] == result.run_id
        assert cast(int, logger.params["model.parameter_count"]) > 0
        assert 0 < cast(float, logger.params["model.gmac_512x512"]) < 1.5
        diagnostic_names = {
            path.name
            for path, artifact_path in logger.artifacts
            if artifact_path == "diagnostics"
        }
        assert {
            "model-summary.json",
            "learning-curve.json",
            "learning-curve.svg",
            "validation-predictions.json",
            "prediction-vs-target.svg",
            "residual.svg",
            "coverage-qq.svg",
        } <= diagnostic_names
        weight_state = cast(Mapping[str, Tensor], weights["state_dict"])
        best_state = cast(Mapping[str, Tensor], best["model_state"])
        assert weight_state.keys() == best_state.keys()
        assert not tuple((tmp_path / "run").glob(".*.tmp"))

        with pytest.raises(ValueError) as error:
            extract_best_weights(
                result.latest_checkpoint,
                tmp_path / "not-best.pt",
                uncertainty_log_variance_offset=0.0,
            )

        assert "best checkpoint" in str(error.value)

    def test_atomic_checkpoint_rejects_invalid_readback_before_replacing_best(
        self, tmp_path: Path
    ):
        path = tmp_path / "best.ckpt"
        valid = {
            "kind": CHECKPOINT_KIND,
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_role": "best",
            "created_unix_seconds": 1.0,
            "run_id": "atomic-run",
            "model_config": {},
            "model_state": {"weight": torch.tensor([1.0])},
            "optimizer_state": {},
            "scheduler_state": {},
            "grad_scaler_state": {},
            "epoch": 1,
            "global_step": 2,
            "next_batch_index": 0,
            "epochs_completed": 1,
            "batch_plan": [],
            "best_selection_state": {},
            "best_validation_metrics": None,
            "rng_state": {},
            "dataset_fingerprint": "sha256:" + "d" * 64,
            "split_fingerprint": "sha256:" + "e" * 64,
            "config_fingerprint": "sha256:" + "c" * 64,
            "training_protocol_fingerprint": "sha256:" + "f" * 64,
            "resolved_config": {},
            "preprocess_schema": {},
            "uncertainty_log_variance_offset": None,
            "train_sample_ids": ["train-0"],
            "trainer_config": {},
            "run_kind": "base-train",
            "parent_run_id": None,
            "parent_checkpoint_id": None,
        }
        atomic_torch_save(valid, path)
        original = path.read_bytes()

        assert load_training_checkpoint(path)["checkpoint_role"] == "best"
        assert not tuple(tmp_path.glob(".*.tmp"))

        invalid_payloads = (
            {**valid, "kind": "another-kind"},
            {**valid, "schema_version": 999},
            {**valid, "checkpoint_role": "candidate"},
            {**valid, "checkpoint_role": "latest"},
            {key: value for key, value in valid.items() if key != "optimizer_state"},
        )
        for invalid in invalid_payloads:
            with pytest.raises(ValueError):
                atomic_torch_save(invalid, path)
            assert path.read_bytes() == original
            assert load_training_checkpoint(path)["checkpoint_role"] == "best"
            assert not tuple(tmp_path.glob(".*.tmp"))

    @pytest.mark.parametrize("criterion", ["nll", "mae-tie"])
    def test_best_checkpoint_uses_nll_first_and_mae_for_ties(
        self, tmp_path: Path, criterion: str
    ):
        data = _TensorTrainingData(
            train_targets={"train-0": 3.0},
            validation_targets={"validation-0": 3.0},
            fail_on_training_call=3,
        )
        directory = tmp_path / criterion
        config = _core_config(directory, data, max_epochs=2)
        with pytest.raises(RuntimeError, match="controlled interruption"):
            train_model(
                config,
                data,
                NullExperimentLogger(run_id=f"{criterion}-selection"),
            )
        reference_data = _TensorTrainingData(
            train_targets={"train-0": 3.0},
            validation_targets={"validation-0": 3.0},
        )
        reference = train_model(
            _core_config(
                tmp_path / f"{criterion}-reference", reference_data, max_epochs=2
            ),
            reference_data,
            NullExperimentLogger(run_id=f"{criterion}-reference"),
        )
        reference_payload = load_training_checkpoint(reference.final_checkpoint)
        reference_model = PasteVolumeResNet(_tiny_model_config())
        reference_model.load_state_dict(
            cast(Mapping[str, Tensor], reference_payload["model_state"]), strict=True
        )
        expected = evaluate_batches(
            reference_model,
            reference_data,
            split="validation",
            device=torch.device("cpu"),
        ).metrics
        latest_checkpoint = directory / "latest.ckpt"
        best_checkpoint = directory / "best.ckpt"
        resume_payload = load_training_checkpoint(latest_checkpoint)
        previous_nll = (
            expected.gaussian_nll + 1.0 if criterion == "nll" else expected.gaussian_nll
        )
        previous_mae = 0.0 if criterion == "nll" else expected.mae_ul + 1.0
        resume_payload["best_selection_state"] = {
            "validation_nll": previous_nll,
            "validation_mae_ul": previous_mae,
            "epoch": -1,
            "patience_counter": 0,
        }
        previous_metrics = dict(
            cast(Mapping[str, object], resume_payload["best_validation_metrics"])
        )
        previous_metrics["gaussian_nll"] = previous_nll
        previous_metrics["mae_ul"] = previous_mae
        resume_payload["best_validation_metrics"] = previous_metrics
        atomic_torch_save(resume_payload, latest_checkpoint)
        previous_best = copy.deepcopy(resume_payload)
        previous_best["checkpoint_role"] = "best"
        atomic_torch_save(previous_best, best_checkpoint)

        resumed_data = _TensorTrainingData(
            train_targets={"train-0": 3.0},
            validation_targets={"validation-0": 3.0},
        )
        resumed = train_model(
            _core_config(
                directory,
                resumed_data,
                resume_checkpoint=latest_checkpoint,
                max_epochs=2,
            ),
            resumed_data,
            NullExperimentLogger(run_id=f"{criterion}-selection"),
        )
        selected = cast(
            Mapping[str, object],
            load_training_checkpoint(resumed.best_checkpoint)["best_selection_state"],
        )

        assert cast(float, selected["validation_nll"]) == pytest.approx(
            expected.gaussian_nll
        )
        assert cast(float, selected["validation_mae_ul"]) == pytest.approx(
            expected.mae_ul
        )


class TestCheckpointResume:
    def test_best_checkpoint_resumes_after_its_completed_epoch(self, tmp_path: Path):
        reference_data = _TensorTrainingData()
        reference = train_model(
            _core_config(tmp_path / "reference-best", reference_data, max_epochs=2),
            reference_data,
            NullExperimentLogger(run_id="best-resume-run"),
        )
        interrupted_data = _TensorTrainingData(fail_on_training_call=5)
        directory = tmp_path / "resume-best"
        with pytest.raises(RuntimeError, match="controlled interruption"):
            train_model(
                _core_config(directory, interrupted_data, max_epochs=2),
                interrupted_data,
                NullExperimentLogger(run_id="best-resume-run"),
            )
        best_path = directory / "best.ckpt"
        best = load_training_checkpoint(best_path)
        assert best["epoch"] == 1
        assert best["epochs_completed"] == 1
        assert best["next_batch_index"] == 0

        resumed_data = _TensorTrainingData()
        resumed = train_model(
            _core_config(
                directory,
                resumed_data,
                resume_checkpoint=best_path,
                max_epochs=2,
            ),
            resumed_data,
            NullExperimentLogger(run_id="best-resume-run"),
        )
        expected = load_training_checkpoint(reference.final_checkpoint)
        actual = load_training_checkpoint(resumed.final_checkpoint)
        for key in (
            "model_state",
            "optimizer_state",
            "scheduler_state",
            "grad_scaler_state",
            "best_selection_state",
            "rng_state",
            "global_step",
            "epochs_completed",
        ):
            _assert_nested_equal(actual[key], expected[key])

    def test_resume_exactly_matches_uninterrupted_model_optimizer_sampler_and_rng(
        self, tmp_path: Path
    ):
        uninterrupted_data = _TensorTrainingData()
        uninterrupted = train_model(
            _core_config(tmp_path / "uninterrupted", uninterrupted_data),
            uninterrupted_data,
            NullExperimentLogger(run_id="same-run"),
        )

        interrupted_data = _TensorTrainingData(fail_on_training_call=3)
        interrupted_config = _core_config(tmp_path / "resumed", interrupted_data)
        with pytest.raises(RuntimeError, match="controlled interruption"):
            train_model(
                interrupted_config,
                interrupted_data,
                NullExperimentLogger(run_id="same-run"),
            )
        interrupted_checkpoint = tmp_path / "resumed" / "latest.ckpt"
        interrupted_payload = load_training_checkpoint(interrupted_checkpoint)
        assert interrupted_payload["global_step"] == 1
        assert interrupted_payload["next_batch_index"] == 1

        resumed_data = _TensorTrainingData()
        resumed = train_model(
            _core_config(
                tmp_path / "resumed",
                resumed_data,
                resume_checkpoint=interrupted_checkpoint,
            ),
            resumed_data,
            NullExperimentLogger(run_id="same-run"),
        )

        uninterrupted_final = load_training_checkpoint(uninterrupted.final_checkpoint)
        resumed_final = load_training_checkpoint(resumed.final_checkpoint)
        for key in (
            "model_state",
            "optimizer_state",
            "scheduler_state",
            "grad_scaler_state",
            "best_selection_state",
            "rng_state",
            "global_step",
            "epochs_completed",
        ):
            _assert_nested_equal(resumed_final[key], uninterrupted_final[key])
        assert resumed.run_id == uninterrupted.run_id == "same-run"
        assert resumed_data.training_requests == [
            (0, ("train-0",)),
            (0, ("train-1",)),
            (0, ("train-2",)),
        ]

    def test_resume_truncates_duplicate_and_future_learning_curve_entries(
        self, tmp_path: Path
    ):
        directory = tmp_path / "history-resume"
        interrupted_data = _TensorTrainingData(fail_on_training_call=5)
        with pytest.raises(RuntimeError, match="controlled interruption"):
            train_model(
                _core_config(directory, interrupted_data, max_epochs=2),
                interrupted_data,
                NullExperimentLogger(run_id="history-run"),
            )
        latest_path = directory / "latest.ckpt"
        latest = load_training_checkpoint(latest_path)
        assert latest["epoch"] == 1
        assert latest["global_step"] == 3
        history_path = directory / "diagnostics" / "learning-curve.json"
        original = json.loads(history_path.read_text(encoding="utf-8"))["epochs"][0]
        valid_duplicate = {**original, "global_step": 3, "marker": "kept"}
        history_path.write_text(
            json.dumps(
                {
                    "epochs": [
                        original,
                        valid_duplicate,
                        {**original, "global_step": 999, "marker": "future-step"},
                        {**original, "epoch": 1, "marker": "future-epoch"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        resumed_data = _TensorTrainingData()
        train_model(
            _core_config(
                directory,
                resumed_data,
                resume_checkpoint=latest_path,
                max_epochs=2,
            ),
            resumed_data,
            NullExperimentLogger(run_id="history-run"),
        )
        epochs = json.loads(history_path.read_text(encoding="utf-8"))["epochs"]

        assert [item["epoch"] for item in epochs] == [0, 1]
        assert epochs[0]["marker"] == "kept"
        assert [item["global_step"] for item in epochs] == [3, 6]

    def test_resume_rejects_dataset_config_and_run_id_mismatch(self, tmp_path: Path):
        interrupted_data = _TensorTrainingData(fail_on_training_call=3)
        base_config = _core_config(tmp_path / "run", interrupted_data)
        with pytest.raises(RuntimeError, match="controlled interruption"):
            train_model(
                base_config,
                interrupted_data,
                NullExperimentLogger(run_id="original-run"),
            )
        checkpoint = tmp_path / "run" / "latest.ckpt"

        for config, message in (
            (
                _core_config(
                    tmp_path / "run",
                    _TensorTrainingData(),
                    resume_checkpoint=checkpoint,
                    dataset_fingerprint="sha256:" + "1" * 64,
                ),
                "dataset_fingerprint",
            ),
            (
                _core_config(
                    tmp_path / "run",
                    _TensorTrainingData(),
                    resume_checkpoint=checkpoint,
                    config_fingerprint="sha256:" + "2" * 64,
                ),
                "config_fingerprint",
            ),
            (
                _core_config(
                    tmp_path / "run",
                    _TensorTrainingData(),
                    resume_checkpoint=checkpoint,
                    training_protocol_fingerprint="sha256:" + "3" * 64,
                ),
                "training_protocol_fingerprint",
            ),
        ):
            with pytest.raises(ValueError) as error:
                train_model(config, _TensorTrainingData(), NullExperimentLogger())
            assert message in str(error.value)

        matching_data = _TensorTrainingData()
        with pytest.raises(ValueError) as error:
            train_model(
                _core_config(
                    tmp_path / "run",
                    matching_data,
                    resume_checkpoint=checkpoint,
                ),
                matching_data,
                NullExperimentLogger(run_id="another-run"),
            )

        assert "original MLflow run ID" in str(error.value)


class TestTrainingFailureRecovery:
    def test_arbitrary_failure_records_latest_reason_and_failed_run(
        self, tmp_path: Path
    ):
        data = _TensorTrainingData(fail_on_training_call=2)
        directory = tmp_path / "arbitrary-failure"
        logger = NullExperimentLogger(run_id="failed-run")

        with pytest.raises(RuntimeError, match="controlled interruption"):
            train_model(_core_config(directory, data), data, logger)

        failure = json.loads((directory / "failure.json").read_text(encoding="utf-8"))
        assert failure["exception_type"] == "RuntimeError"
        assert failure["message"] == "controlled interruption"
        assert (
            load_training_checkpoint(directory / "latest.ckpt")["checkpoint_role"]
            == "latest"
        )
        assert logger.status == "FAILED"
        assert logger.tags["failure_reason"] == "RuntimeError"
        assert {
            (directory / "latest.ckpt", "checkpoints"),
            (directory / "failure.json", "failure"),
        } <= set(logger.artifacts)

    def test_partial_accumulation_exception_publishes_exact_retry_boundary(
        self, tmp_path: Path
    ):
        targets = {"train-0": 1.5, "train-1": 1.75}
        failing_data = _TensorTrainingData(
            train_targets=targets,
            fail_on_training_call=3,
        )
        directory = tmp_path / "partial-group"
        config = _core_config(
            directory,
            failing_data,
            gradient_accumulation_steps=2,
        )

        with pytest.raises(RuntimeError, match="controlled interruption"):
            train_model(
                config,
                failing_data,
                NullExperimentLogger(run_id="partial-group-run"),
            )

        latest = load_training_checkpoint(directory / "latest.ckpt")
        emergency = load_training_checkpoint(directory / "emergency.ckpt")
        failure = json.loads((directory / "failure.json").read_text(encoding="utf-8"))
        assert failure["global_step"] == latest["global_step"] == 0
        assert latest["next_batch_index"] == emergency["next_batch_index"] == 0
        for key in (
            "model_state",
            "optimizer_state",
            "scheduler_state",
            "grad_scaler_state",
            "rng_state",
            "global_step",
            "next_batch_index",
        ):
            _assert_nested_equal(latest[key], emergency[key])

        resumed_data = _TensorTrainingData(train_targets=targets)
        resumed = train_model(
            _core_config(
                directory,
                resumed_data,
                resume_checkpoint=directory / "emergency.ckpt",
                gradient_accumulation_steps=2,
            ),
            resumed_data,
            NullExperimentLogger(run_id="partial-group-run"),
        )
        uninterrupted_data = _TensorTrainingData(train_targets=targets)
        uninterrupted = train_model(
            _core_config(
                tmp_path / "uninterrupted-group",
                uninterrupted_data,
                gradient_accumulation_steps=2,
            ),
            uninterrupted_data,
            NullExperimentLogger(run_id="partial-group-run"),
        )
        resumed_final = load_training_checkpoint(resumed.final_checkpoint)
        uninterrupted_final = load_training_checkpoint(uninterrupted.final_checkpoint)
        for key in (
            "model_state",
            "optimizer_state",
            "scheduler_state",
            "grad_scaler_state",
            "rng_state",
            "global_step",
            "epochs_completed",
        ):
            _assert_nested_equal(resumed_final[key], uninterrupted_final[key])

    @pytest.mark.parametrize("failure_kind", ["loss", "gradient"])
    def test_nonfinite_accumulation_group_writes_a_retryable_emergency_checkpoint(
        self, tmp_path: Path, failure_kind: Literal["loss", "gradient"]
    ):
        failing_data = _TensorTrainingData(
            train_targets={"train-0": 1.5, "train-1": 1.75},
            nonfinite_on_training_call=(3, failure_kind),
        )
        directory = tmp_path / failure_kind
        config = _core_config(
            directory,
            failing_data,
            gradient_accumulation_steps=2,
        )

        with pytest.raises(FloatingPointError) as error:
            train_model(
                config,
                failing_data,
                NullExperimentLogger(run_id=f"{failure_kind}-run"),
            )

        assert "accumulation group rolled back" in str(error.value)
        emergency = directory / "emergency.ckpt"
        payload = load_training_checkpoint(emergency)
        assert payload["checkpoint_role"] == "emergency"
        assert payload["global_step"] == 0
        assert payload["next_batch_index"] == 0

        seed_everything(config.trainer.seed, deterministic=True)
        initial_model = PasteVolumeResNet(config.model)
        _assert_nested_equal(payload["model_state"], initial_model.state_dict())

        retry_data = _TensorTrainingData(
            train_targets={"train-0": 1.5, "train-1": 1.75}
        )
        retried = train_model(
            _core_config(
                directory,
                retry_data,
                resume_checkpoint=emergency,
                gradient_accumulation_steps=2,
            ),
            retry_data,
            NullExperimentLogger(run_id=f"{failure_kind}-run"),
        )

        assert retried.global_step == 1
        assert retry_data.training_requests == [
            (0, ("train-0",)),
            (0, ("train-0",)),
            (0, ("train-1",)),
        ]


class TestFineTuning:
    def test_finetune_inherits_strict_lineage_and_changes_only_trainable_layers(
        self, tmp_path: Path
    ):
        base_data = _TensorTrainingData(
            train_targets={"train-0": 2.0},
            validation_targets={"validation-0": 2.0},
        )
        base = train_model(
            _core_config(tmp_path / "base", base_data),
            base_data,
            NullExperimentLogger(run_id="base-run"),
        )
        base_weights = cast(
            dict[str, object],
            torch.load(base.weights_path, map_location="cpu", weights_only=True),
        )

        fine_data = _TensorTrainingData(
            train_targets={"train-0": 3.0},
            validation_targets={"validation-0": 3.0},
        )
        fine = train_model(
            _core_config(
                tmp_path / "fine",
                fine_data,
                run_kind="finetune",
                initial_weights=base.weights_path,
                parent_base_run_id="base-run",
            ),
            fine_data,
            NullExperimentLogger(run_id="fine-run"),
        )
        final = load_training_checkpoint(fine.final_checkpoint)

        assert fine.parent_run_id == "base-run"
        assert fine.parent_checkpoint_id == base_weights["source_checkpoint_sha256"]
        assert final["parent_run_id"] == "base-run"
        assert final["parent_checkpoint_id"] == base_weights["source_checkpoint_sha256"]

        trainability = PasteVolumeResNet(_tiny_model_config())
        trainability.set_fine_tune_trainable(full_model=False)
        expected_trainable = {
            name
            for name, parameter in trainability.named_parameters()
            if parameter.requires_grad
        }
        expected_frozen = {
            name
            for name, parameter in trainability.named_parameters()
            if not parameter.requires_grad
        }
        initial_state = cast(Mapping[str, Tensor], base_weights["state_dict"])
        final_state = cast(Mapping[str, Tensor], final["model_state"])
        assert expected_trainable
        assert expected_frozen
        assert all(
            torch.equal(initial_state[name], final_state[name])
            for name in expected_frozen
        )
        assert any(
            not torch.equal(initial_state[name], final_state[name])
            for name in expected_trainable
        )

    def test_finetune_resume_is_self_contained_after_initial_weights_are_removed(
        self, tmp_path: Path
    ):
        base_data = _TensorTrainingData(
            train_targets={"train-0": 2.0},
            validation_targets={"validation-0": 2.0},
        )
        base = train_model(
            _core_config(tmp_path / "base", base_data),
            base_data,
            NullExperimentLogger(run_id="base-run"),
        )
        fine_data = _TensorTrainingData(
            train_targets={"train-0": 3.0},
            validation_targets={"validation-0": 3.0},
        )
        fine_directory = tmp_path / "fine"
        first = train_model(
            _core_config(
                fine_directory,
                fine_data,
                run_kind="finetune",
                initial_weights=base.weights_path,
                parent_base_run_id="base-run",
                max_epochs=2,
                max_steps=1,
            ),
            fine_data,
            NullExperimentLogger(run_id="fine-run"),
        )
        base.weights_path.unlink()

        resumed = train_model(
            _core_config(
                fine_directory,
                fine_data,
                run_kind="finetune",
                resume_checkpoint=first.latest_checkpoint,
                max_epochs=2,
                max_steps=1,
            ),
            fine_data,
            NullExperimentLogger(run_id="fine-run"),
        )

        assert resumed.run_id == "fine-run"
        assert resumed.parent_run_id == "base-run"
        assert resumed.parent_checkpoint_id == first.parent_checkpoint_id

    def test_gpu_option_can_finetune_the_full_model(self, tmp_path: Path):
        base_data = _TensorTrainingData(
            train_targets={"train-0": 2.0},
            validation_targets={"validation-0": 2.0},
        )
        base = train_model(
            _core_config(tmp_path / "full-base", base_data),
            base_data,
            NullExperimentLogger(run_id="full-base-run"),
        )
        fine_data = _TensorTrainingData(
            train_targets={"train-0": 3.0},
            validation_targets={"validation-0": 3.0},
        )
        fine = train_model(
            _core_config(
                tmp_path / "full-fine",
                fine_data,
                run_kind="finetune",
                initial_weights=base.weights_path,
                parent_base_run_id="full-base-run",
                fine_tune_full_model=True,
            ),
            fine_data,
            NullExperimentLogger(run_id="full-fine-run"),
        )
        optimizer_state = cast(
            Mapping[str, object],
            load_training_checkpoint(fine.final_checkpoint)["optimizer_state"],
        )
        parameter_groups = cast(
            list[Mapping[str, object]], optimizer_state["param_groups"]
        )
        optimized_parameters = cast(list[int], parameter_groups[0]["params"])
        assert len(optimized_parameters) == len(
            tuple(PasteVolumeResNet(_tiny_model_config()).parameters())
        )

    @pytest.mark.parametrize(
        ("mutation", "message"),
        [
            (lambda payload: payload.update(kind="wrong-kind"), "unsupported"),
            (
                lambda payload: payload.update(weight_schema_version=999),
                "unsupported",
            ),
            (
                lambda payload: payload.update(source_checkpoint_role="final"),
                "best checkpoint",
            ),
            (
                lambda payload: payload.pop("dataset_fingerprint"),
                "key set",
            ),
            (
                lambda payload: cast(
                    dict[str, Tensor], payload["state_dict"]
                ).popitem(),
                "state_dict",
            ),
        ],
    )
    def test_finetune_rejects_malformed_initial_weights(
        self,
        tmp_path: Path,
        mutation: Callable[[dict[str, object]], object],
        message: str,
    ):
        base_data = _TensorTrainingData(
            train_targets={"train-0": 2.0},
            validation_targets={"validation-0": 2.0},
        )
        base = train_model(
            _core_config(tmp_path / "base", base_data),
            base_data,
            NullExperimentLogger(run_id="base-run"),
        )
        payload = cast(
            dict[str, object],
            torch.load(base.weights_path, map_location="cpu", weights_only=True),
        )
        mutation(payload)
        malformed = tmp_path / "malformed.pt"
        torch.save(payload, malformed)
        fine_data = _TensorTrainingData(
            train_targets={"train-0": 3.0},
            validation_targets={"validation-0": 3.0},
        )

        with pytest.raises((ValueError, RuntimeError)) as error:
            train_model(
                _core_config(
                    tmp_path / "fine",
                    fine_data,
                    run_kind="finetune",
                    initial_weights=malformed,
                    parent_base_run_id="base-run",
                ),
                fine_data,
                NullExperimentLogger(run_id="fine-run"),
            )

        assert message in str(error.value)

    def test_finetune_rejects_declared_parent_run_mismatch(self, tmp_path: Path):
        base_data = _TensorTrainingData(
            train_targets={"train-0": 2.0},
            validation_targets={"validation-0": 2.0},
        )
        base = train_model(
            _core_config(tmp_path / "base", base_data),
            base_data,
            NullExperimentLogger(run_id="base-run"),
        )
        fine_data = _TensorTrainingData(
            train_targets={"train-0": 3.0},
            validation_targets={"validation-0": 3.0},
        )

        with pytest.raises(ValueError) as error:
            train_model(
                _core_config(
                    tmp_path / "fine",
                    fine_data,
                    run_kind="finetune",
                    initial_weights=base.weights_path,
                    parent_base_run_id="different-base-run",
                ),
                fine_data,
                NullExperimentLogger(run_id="fine-run"),
            )

        assert "parent_base_run_id" in str(error.value)


class TestFormalTrainingWeights:
    def test_hydra_training_publishes_verifiable_real_mlflow_weights(
        self, tmp_path: Path
    ):
        from mlflow import MlflowClient
        from omegaconf import OmegaConf

        dataset = tmp_path / "dataset"
        for index in range(6):
            write_synthetic_session(
                dataset,
                f"formal-session-{index}",
                machine_id=f"machine-{index}",
                pad_count=1,
                width=40,
                height=40,
                content_seed=index,
            )
        server = _MLflowServer(tmp_path / "mlflow-server")
        server.start()
        tracking_uri = server.uri
        raw = _raw_train_mapping(tmp_path)
        data_raw = cast(dict[str, object], raw["data"])
        data_raw["manifest"] = None
        data_raw["roots"] = [str(dataset)]
        checkpoint_raw = cast(dict[str, object], raw["checkpoint"])
        checkpoint_raw["directory"] = str(tmp_path / "hydra-run")
        trainer_raw = cast(dict[str, object], raw["trainer"])
        trainer_raw["max_steps"] = 1
        logger_raw = cast(dict[str, object], raw["logger"])
        logger_raw["tracking_uri"] = tracking_uri
        logger_raw["experiment_name"] = "formal-hydra-training"
        raw["repository_root"] = str(Path.cwd())

        try:
            result = hydra_train(OmegaConf.create(raw))
            verified = load_formal_training_weights(
                result.weights_path,
                tracking_uri=tracking_uri,
            )
            run = MlflowClient(tracking_uri=tracking_uri).get_run(result.run_id)

            assert run.info.status == "FINISHED"
            assert verified.source_run_id == result.run_id
            assert verified.run_kind == "base-train"
            assert verified.weights_path == result.weights_path.resolve()
            assert formal_artifact_attestation_path(result.weights_path).is_file()
        finally:
            server.stop()

    def test_hpo_trial_run_is_not_a_formal_weights_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from mlflow import MlflowClient

        data = _TensorTrainingData()
        tracking_uri, logger = _formal_logger(
            tmp_path,
            monkeypatch,
            experiment_name="formal-hpo-rejection",
        )
        config = replace(
            _core_config(tmp_path / "hpo-run", data),
            run_tags={"hpo.study_name": "study", "hpo.trial_number": "3"},
        )

        result = train_model(
            config,
            data,
            logger,
            formal_tracking_uri=tracking_uri,
        )

        assert formal_artifact_attestation_path(result.weights_path).is_file()
        with pytest.raises(ValueError, match="HPO trial"):
            load_formal_training_weights(
                result.weights_path,
                tracking_uri=tracking_uri,
            )
        client = MlflowClient(tracking_uri=tracking_uri)
        client.delete_tag(result.run_id, "hpo.study_name")
        client.delete_tag(result.run_id, "hpo.trial_number")
        client.set_tag(
            result.run_id,
            "dataset_fingerprint",
            "sha256:" + "0" * 64,
        )
        with pytest.raises(ValueError, match="dataset_fingerprint"):
            load_formal_training_weights(
                result.weights_path,
                tracking_uri=tracking_uri,
            )

    def test_formal_finetune_requires_and_accepts_only_attested_base_weights(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        base_data = _TensorTrainingData(
            train_targets={"train-0": 2.0},
            validation_targets={"validation-0": 2.0},
        )
        tracking_uri, base_logger = _formal_logger(
            tmp_path,
            monkeypatch,
            experiment_name="formal-finetune",
        )
        base = train_model(
            _core_config(tmp_path / "base", base_data),
            base_data,
            base_logger,
            formal_tracking_uri=tracking_uri,
        )
        fine_data = _TensorTrainingData(
            train_targets={"train-0": 3.0},
            validation_targets={"validation-0": 3.0},
        )
        _, fine_logger = _formal_logger(
            tmp_path,
            monkeypatch,
            experiment_name="formal-finetune",
        )

        fine = train_model(
            _core_config(
                tmp_path / "fine",
                fine_data,
                run_kind="finetune",
                initial_weights=base.weights_path,
                parent_base_run_id=base.run_id,
            ),
            fine_data,
            fine_logger,
            formal_tracking_uri=tracking_uri,
        )
        verified = load_formal_training_weights(
            fine.weights_path,
            tracking_uri=tracking_uri,
        )

        assert verified.run_kind == "finetune"
        assert verified.source_run_id == fine.run_id
        assert fine.parent_run_id == base.run_id

    def test_forged_unattested_base_weights_fail_before_finetune_run_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        base_data = _TensorTrainingData(
            train_targets={"train-0": 2.0},
            validation_targets={"validation-0": 2.0},
        )
        raw_base = train_model(
            _core_config(tmp_path / "raw-base", base_data),
            base_data,
            NullExperimentLogger(run_id="raw-base-run"),
        )
        tracking_uri, logger = _formal_logger(
            tmp_path,
            monkeypatch,
            experiment_name="forged-finetune",
        )
        fine_data = _TensorTrainingData(
            train_targets={"train-0": 3.0},
            validation_targets={"validation-0": 3.0},
        )

        with pytest.raises(ValueError, match="attestation is missing"):
            train_model(
                _core_config(
                    tmp_path / "forged-fine",
                    fine_data,
                    run_kind="finetune",
                    initial_weights=raw_base.weights_path,
                    parent_base_run_id=raw_base.run_id,
                ),
                fine_data,
                logger,
                formal_tracking_uri=tracking_uri,
            )

        from mlflow import MlflowClient

        assert (
            MlflowClient(tracking_uri=tracking_uri).get_experiment_by_name(
                "forged-finetune"
            )
            is None
        )

    def test_started_failure_ends_failed_without_formal_receipt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from mlflow import MlflowClient

        data = _TensorTrainingData(fail_on_training_call=2)
        tracking_uri, logger = _formal_logger(
            tmp_path,
            monkeypatch,
            experiment_name="formal-training-failure",
        )
        directory = tmp_path / "failed"

        with pytest.raises(RuntimeError, match="controlled interruption"):
            train_model(
                _core_config(directory, data),
                data,
                logger,
                formal_tracking_uri=tracking_uri,
            )

        client = MlflowClient(tracking_uri=tracking_uri)
        experiment = client.get_experiment_by_name("formal-training-failure")
        assert experiment is not None
        runs = client.search_runs([experiment.experiment_id])
        assert len(runs) == 1
        assert runs[0].info.status == "FAILED"
        assert not formal_artifact_attestation_path(directory / "weights.pt").exists()
        assert (directory / "failure.json").is_file()


class TestCompileTrainingSmoke:
    def test_compile_smoke_reports_zero_graph_breaks(self, tmp_path: Path):
        data = _TensorTrainingData()
        logger = NullExperimentLogger(run_id="compile-smoke-run")

        train_model(
            _core_config(
                tmp_path / "compile-smoke",
                data,
                compile_enabled=True,
                compile_backend="eager",
            ),
            data,
            logger,
        )

        compile_metrics = next(
            metrics
            for _, metrics in logger.metrics
            if "compile_graph_break_count" in metrics
        )
        assert compile_metrics["compile_graph_break_count"] == 0.0
        assert logger.status == "FINISHED"


class TestTrainingStopConditions:
    def test_deadline_crossed_by_eager_smoke_skips_compile(self, tmp_path: Path):
        data = _TensorTrainingData(training_batch_delay_seconds=1.1)
        logger = NullExperimentLogger(run_id="compile-deadline-run")
        directory = tmp_path / "compile-deadline"

        with pytest.raises(TrainingDeadlineExceeded):
            train_model(
                _core_config(
                    directory,
                    data,
                    deadline_seconds=0.01,
                    compile_enabled=True,
                    compile_backend="eager",
                ),
                data,
                logger,
                deadline_started_at=time.monotonic() + 1.0,
            )

        compile_metrics = next(
            metrics
            for _, metrics in logger.metrics
            if "compile_skipped_deadline" in metrics
        )
        assert data.training_call_count == 1
        assert compile_metrics["compile_skipped_deadline"] == 1.0
        assert compile_metrics["compile_seconds"] == 0.0

    def test_entrypoint_hard_deadline_fails_with_only_a_resume_checkpoint(
        self, tmp_path: Path
    ):
        data = _TensorTrainingData()
        logger = NullExperimentLogger(run_id="hard-deadline-run")
        directory = tmp_path / "hard-deadline"

        with pytest.raises(TimeoutError, match="finalization deadline"):
            train_model(
                _core_config(
                    directory,
                    data,
                    deadline_seconds=1.0,
                    finalization_grace_seconds=0.1,
                ),
                data,
                logger,
                deadline_started_at=time.monotonic() - 10.0,
            )

        assert (
            load_training_checkpoint(directory / "latest.ckpt")["checkpoint_role"]
            == "latest"
        )
        assert (directory / "failure.json").is_file()
        assert not (directory / "best.ckpt").exists()
        assert not (directory / "final.ckpt").exists()
        assert not (directory / "weights.pt").exists()
        assert logger.status == "FAILED"

    def test_signal_during_final_calibration_does_not_publish_final_weights(
        self, tmp_path: Path
    ):
        data = _TensorTrainingData(
            validation_targets={"validation-0": 1.6, "validation-1": 1.9},
            evaluation_plan=(("validation-0",), ("validation-1",)),
            signal_on_evaluation_call=3,
        )
        logger = NullExperimentLogger(run_id="finalization-signal-run")
        directory = tmp_path / "finalization-signal"

        with pytest.raises(TrainingInterrupted):
            train_model(_core_config(directory, data), data, logger)

        assert (
            load_training_checkpoint(directory / "latest.ckpt")["checkpoint_role"]
            == "latest"
        )
        assert (directory / "best.ckpt").is_file()
        assert not (directory / "final.ckpt").exists()
        assert not (directory / "weights.pt").exists()
        assert logger.status == "KILLED"
        assert data.evaluation_call_count == 3

    def test_signal_during_validation_stops_before_materializing_the_next_batch(
        self, tmp_path: Path
    ):
        data = _TensorTrainingData(
            validation_targets={"validation-0": 1.6, "validation-1": 1.9},
            evaluation_plan=(("validation-0",), ("validation-1",)),
            signal_on_evaluation_call=1,
        )
        logger = NullExperimentLogger(run_id="validation-signal-run")
        directory = tmp_path / "validation-signal"

        with pytest.raises(TrainingInterrupted):
            train_model(_core_config(directory, data), data, logger)

        assert data.evaluation_call_count == 1
        assert (
            load_training_checkpoint(directory / "latest.ckpt")["checkpoint_role"]
            == "latest"
        )
        assert not (directory / "best.ckpt").exists()
        assert not (directory / "final.ckpt").exists()
        assert not (directory / "weights.pt").exists()
        assert logger.status == "KILLED"

    @pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGTERM])
    def test_sigterm_publishes_only_latest_and_marks_the_run_killed(
        self, tmp_path: Path, termination_signal: signal.Signals
    ):
        data = _TensorTrainingData(
            signal_on_training_call=4,
            termination_signal=termination_signal,
        )
        logger = NullExperimentLogger(run_id="signal-run")
        directory = tmp_path / "signal"

        with pytest.raises(TrainingInterrupted) as interrupted:
            train_model(_core_config(directory, data), data, logger)

        latest = load_training_checkpoint(interrupted.value.checkpoint)
        assert latest["checkpoint_role"] == "latest"
        assert latest["next_batch_index"] == 3
        assert logger.status == "KILLED"
        assert logger.tags["stop_reason"] == "signal"
        assert not (directory / "best.ckpt").exists()
        assert not (directory / "final.ckpt").exists()
        assert not (directory / "weights.pt").exists()

    def test_max_steps_stops_at_optimizer_boundary_and_preserves_next_batch(
        self, tmp_path: Path
    ):
        data = _TensorTrainingData()

        result = train_model(
            _core_config(tmp_path / "steps", data, max_steps=1),
            data,
            NullExperimentLogger(run_id="steps-run"),
        )
        final = load_training_checkpoint(result.final_checkpoint)

        assert result.stopped_reason == "max_steps"
        assert result.global_step == 1
        assert final["next_batch_index"] == 1

    def test_expired_deadline_stops_before_the_first_optimizer_step(
        self, tmp_path: Path
    ):
        data = _TensorTrainingData()
        logger = NullExperimentLogger(run_id="deadline-run")
        directory = tmp_path / "deadline"

        with pytest.raises(TrainingDeadlineExceeded) as expired:
            train_model(
                _core_config(directory, data, deadline_seconds=1e-9),
                data,
                logger,
            )
        latest = load_training_checkpoint(expired.value.checkpoint)

        assert latest["global_step"] == 0
        assert latest["epochs_completed"] == 0
        assert latest["checkpoint_role"] == "latest"
        assert latest["next_batch_index"] == 0
        assert logger.status == "KILLED"
        assert not (directory / "best.ckpt").exists()
        assert not (directory / "final.ckpt").exists()
        assert not (directory / "weights.pt").exists()


class TestSessionBalancedSubsetArtifact:
    def test_formal_train_records_a_deterministic_session_balanced_subset(
        self, tmp_path: Path
    ):
        dataset_root = tmp_path / "dataset"
        dataset_root.mkdir()
        for session_index in range(6):
            write_synthetic_session(
                dataset_root,
                f"session-{session_index}",
                machine_id=f"machine-{session_index}",
                pad_count=1,
                width=32,
                height=32,
            )
        run_directory = tmp_path / "formal-run"
        config = TrainConfig(
            data=DataConfig(
                roots=(dataset_root,),
                constraints=ImageConstraints(
                    min_size=32,
                    max_size=64,
                    max_pixels=4096,
                    stride=32,
                ),
                augmentation=AugmentationConfig(enabled=False),
                max_batch_pixels=8192,
                max_batch_size=2,
            ),
            checkpoint=CheckpointConfig(
                directory=run_directory,
                save_interval_steps=1,
                save_interval_seconds=3600.0,
            ),
            model=_tiny_model_config(),
            trainer=TrainerConfig(
                device="cpu",
                seed=23,
                learning_rate=1e-2,
                weight_decay=0.0,
                max_epochs=1,
                early_stopping_patience=2,
                scheduler_patience=2,
                compile_enabled=False,
                deterministic=True,
                max_train_samples=2,
            ),
            repository_root=Path.cwd(),
        )

        first_prepared = prepare_training_data(config)
        second_prepared = prepare_training_data(config)
        logger = NullExperimentLogger(run_id="formal-run")
        result = train(config, logger)
        recorded = json.loads(
            (run_directory / "selected-train-samples.json").read_text(encoding="utf-8")
        )
        dataset_validation_path = run_directory / "dataset-validation.json"
        dataset_validation = json.loads(
            dataset_validation_path.read_text(encoding="utf-8")
        )
        selected = first_prepared.selected_train_samples

        assert tuple(sample.sample_id for sample in selected) == tuple(
            sample.sample_id for sample in second_prepared.selected_train_samples
        )
        assert recorded["sample_ids"] == [sample.sample_id for sample in selected]
        assert recorded["count"] == 2
        assert len({sample.session_id for sample in selected}) == 2
        assert result.latest_checkpoint.is_file()
        assert logger.params["data.max_batch_pixels"] == 8192
        assert logger.params["preprocess.normalization.axes"] == "[0, 1, 2]"
        assert isinstance(logger.params["dependencies.torch"], str)
        assert dataset_validation == {
            "composite_fingerprint": first_prepared.composite.composite_fingerprint,
            "content_fingerprint": first_prepared.composite.content_fingerprint,
            "sample_count": len(first_prepared.samples),
            "session_count": len(first_prepared.composite.sessions),
            "source_count": len(first_prepared.composite.sources),
        }
        assert (dataset_validation_path, "provenance") in logger.artifacts
        assert json.loads(cast(str, logger.tags["machine_ids"])) == [
            f"machine-{index}" for index in range(6)
        ]
        final = load_training_checkpoint(result.final_checkpoint)
        resolved = cast(Mapping[str, object], final["resolved_config"])
        resolved_data = cast(Mapping[str, object], resolved["data"])
        assert resolved_data["max_batch_pixels"] == 8192


class TestTrainingConfigBoundary:
    @pytest.mark.parametrize(
        "tracking_uri",
        ("sqlite:////tmp/mlflow.db", "file:///tmp/mlruns"),
    )
    def test_formal_logger_config_requires_http_tracking_server(
        self, tracking_uri: str
    ):
        with pytest.raises(ValueError, match="HTTP"):
            LoggerConfig(tracking_uri=tracking_uri)

    @pytest.mark.parametrize(
        ("value", "expected"),
        (
            (
                "endpoint = http://127.0.0.1:{self._port}",
                "endpoint = http://127.0.0.1:{self._port}",
            ),
            (
                "https://user:secret@example.invalid:{port}/mlflow?token=x#part",
                "https://example.invalid:{port}/mlflow",
            ),
        ),
    )
    def test_uri_sanitization_tolerates_template_ports(self, value: str, expected: str):
        assert sanitize_persisted_text(value) == expected

    def test_persisted_git_diff_keeps_identity_without_persisting_content(self):
        secret_diff = (
            "+ tracking_uri=https://user:password@example.invalid/mlflow?token=x"
        )

        summary = summarize_persisted_git_diff(secret_diff)

        assert summary.startswith("[omitted at persistence boundary; sha256:")
        assert f"bytes:{len(secret_diff.encode('utf-8'))}]" in summary
        assert secret_diff not in summary
        assert "password" not in summary
        assert summary != summarize_persisted_git_diff(secret_diff + "\n")

    def test_training_protocol_fingerprint_is_path_and_assignment_independent(
        self, tmp_path: Path
    ):
        config, _ = train_config_from_mapping(
            _raw_train_mapping(tmp_path), base_directory=tmp_path
        )
        expected = training_protocol_fingerprint(config)
        relocated = replace(
            config,
            data=replace(
                config.data,
                manifest=tmp_path / "other-dataset.json",
                split_manifest=tmp_path / "other-split.json",
                split_seed=999,
                train_ratio=0.6,
                validation_ratio=0.2,
                test_ratio=0.2,
            ),
            checkpoint=replace(
                config.checkpoint,
                directory=tmp_path / "other-run",
                resume_checkpoint=None,
            ),
            repository_root=tmp_path / "other-repository",
            hydra_resolved_yaml=(
                "hydra.sweeper.storage=postgresql://user:secret@db/study?ssl=1"
            ),
            hydra_overrides=("trainer.max_epochs=1",),
        )

        assert training_protocol_fingerprint(relocated) == expected

    def test_training_protocol_fingerprint_covers_all_training_semantics(
        self, tmp_path: Path
    ):
        config, _ = train_config_from_mapping(
            _raw_train_mapping(tmp_path), base_directory=tmp_path
        )
        expected = training_protocol_fingerprint(config)
        variants = (
            replace(config, model=replace(config.model, hidden_features=8)),
            replace(
                config,
                data=replace(
                    config.data,
                    constraints=ImageConstraints(
                        **{
                            **config.data.constraints.to_dict(),
                            "max_size": 512,
                        }
                    ),
                ),
            ),
            replace(
                config,
                data=replace(
                    config.data,
                    augmentation=AugmentationConfig(
                        enabled=True,
                        min_scale=config.data.augmentation.min_scale,
                        max_scale=config.data.augmentation.max_scale,
                    ),
                ),
            ),
            replace(
                config,
                data=replace(config.data, max_batch_size=8),
            ),
            replace(config, trainer=replace(config.trainer, seed=20)),
            replace(
                config,
                run_kind="finetune",
                checkpoint=replace(
                    config.checkpoint, initial_weights=tmp_path / "parent.pt"
                ),
            ),
        )

        assert all(
            training_protocol_fingerprint(variant) != expected for variant in variants
        )

    def test_persisted_artifacts_and_logger_events_do_not_contain_uri_secrets(
        self, tmp_path: Path
    ):
        dataset = tmp_path / "dataset"
        for index in range(4):
            write_synthetic_session(
                dataset,
                f"secret-test-session-{index}",
                pad_count=1,
                width=40,
                height=40,
                content_seed=index,
            )
        storage_uri = (
            "postgresql://leak-user:leak-password@db.internal/study"
            "?sslmode=leak-query#leak-fragment"
        )
        tracking_uri = (
            "https://track-user:track-password@mlflow.internal/db"
            "?token=track-query#track-fragment"
        )
        run_directory = tmp_path / "secret-run"
        config = TrainConfig(
            data=DataConfig(
                roots=(dataset,),
                constraints=ImageConstraints(
                    min_size=32,
                    max_size=64,
                    max_pixels=4_096,
                    stride=32,
                ),
                augmentation=AugmentationConfig(enabled=False),
                max_batch_pixels=8_192,
                max_batch_size=2,
            ),
            checkpoint=CheckpointConfig(
                directory=run_directory,
                save_interval_steps=100,
                save_interval_seconds=300.0,
            ),
            model=_tiny_model_config(),
            trainer=TrainerConfig(
                device="cpu",
                seed=17,
                max_epochs=1,
                max_steps=1,
                early_stopping_patience=2,
                scheduler_patience=2,
                compile_enabled=False,
                deterministic=True,
            ),
            repository_root=Path(__file__).parents[3],
            hpo=HpoTrialConfig(
                study_name="secret-redaction",
                trial_number=0,
                search_config_fingerprint="sha256:" + "a" * 64,
                storage_uri_redacted=sanitize_persisted_uri(storage_uri),
            ),
            hydra_resolved_yaml=(
                f"hydra:\n  sweeper:\n    storage: {storage_uri}\n"
                f"logger:\n  tracking_uri: {tracking_uri}\n"
            ),
            hydra_overrides=(
                f"hydra.sweeper.storage={storage_uri}",
                f"logger.tracking_uri={tracking_uri}",
            ),
        )
        logger = NullExperimentLogger("secret-redaction-run")

        train(config, logger)

        persisted = b"\n".join(
            path.read_bytes()
            for path in sorted(run_directory.rglob("*"))
            if path.is_file()
        )
        logger_payload = json.dumps(
            {
                "params": logger.params,
                "tags": logger.tags,
                "metrics": logger.metrics,
                "artifacts": [
                    [str(path), artifact_path]
                    for path, artifact_path in logger.artifacts
                ],
            },
            sort_keys=True,
        ).encode("utf-8")
        forbidden = (
            "leak-user",
            "leak-password",
            "leak-query",
            "leak-fragment",
            "track-user",
            "track-password",
            "track-query",
            "track-fragment",
        )
        assert all(secret.encode("utf-8") not in persisted for secret in forbidden)
        assert all(secret.encode("utf-8") not in logger_payload for secret in forbidden)

    @pytest.mark.parametrize(
        "location",
        [
            "root",
            "data",
            "constraints",
            "augmentation",
            "checkpoint",
            "logger",
            "model",
            "trainer",
        ],
    )
    def test_unknown_config_keys_are_rejected(self, tmp_path: Path, location: str):
        raw = _raw_train_mapping(tmp_path)
        if location == "root":
            raw["surprise"] = True
        else:
            if location in ("constraints", "augmentation"):
                data = cast(dict[str, object], raw["data"])
                container = cast(dict[str, object], data[location])
            else:
                container = cast(dict[str, object], raw[location])
            container["surprise"] = True

        with pytest.raises(ValueError) as error:
            train_config_from_mapping(raw, base_directory=tmp_path)

        assert "unknown" in str(error.value)
        assert "surprise" in str(error.value)

    def test_relative_paths_are_resolved_at_the_frozen_config_boundary(
        self, tmp_path: Path
    ):
        raw = _raw_train_mapping(tmp_path)
        data = cast(dict[str, object], raw["data"])
        checkpoint = cast(dict[str, object], raw["checkpoint"])
        data["manifest"] = "datasets/composite.json"
        checkpoint["directory"] = "runs/train"
        raw["repository_root"] = "."

        config, _ = train_config_from_mapping(raw, base_directory=tmp_path)

        assert config.data.manifest == (tmp_path / "datasets/composite.json").resolve()
        assert config.checkpoint.directory == (tmp_path / "runs/train").resolve()
        assert config.repository_root == tmp_path.resolve()
        assert preprocess_schema(config.data.constraints)["input_channels"] == 6
