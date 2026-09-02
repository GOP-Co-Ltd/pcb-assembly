"""Persistent Optuna identity and Hydra sweeper contract tests."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import optuna
import pytest
import yaml
from hydra import compose, initialize_config_module
from hydra.utils import instantiate
from mlflow import MlflowClient
from omegaconf import OmegaConf

from pcbasm.pasting.paste_volume.hpo import (
    build_optuna_study_name,
    build_trial_metadata_overrides,
    optuna_trials_payload,
    redact_storage_uri,
    search_config_fingerprint,
    validate_optuna_storage,
    write_optimization_results,
)
from pcbasm.pasting.paste_volume.hpo_sweeper import PersistentOptunaSweeper
from tests.pcbasm.pasting.paste_volume.support_data import write_synthetic_session


def _free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _start_mlflow_server(root: Path) -> tuple[subprocess.Popen[str], str]:
    root.mkdir(parents=True)
    port = _free_port()
    uri = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        (
            sys.executable,
            "-m",
            "mlflow",
            "server",
            "--backend-store-uri",
            f"sqlite:///{root / 'mlflow.db'}",
            "--default-artifact-root",
            (root / "artifacts").as_uri(),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
        ),
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("MLflow server exited during startup")
        try:
            with urlopen(uri + "/health", timeout=0.5) as response:
                if response.status == 200:
                    return process, uri
        except OSError:
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=10)
    raise RuntimeError("MLflow server did not become ready")


class TestStudyIdentity:
    def test_search_fingerprint_is_mapping_order_independent(self):
        first = {"learning_rate": [1e-5, 1e-3], "groups": [1, 4, 8]}
        second = {"groups": [1, 4, 8], "learning_rate": [1e-5, 1e-3]}

        assert search_config_fingerprint(first) == search_config_fingerprint(second)

    def test_study_name_changes_with_each_identity_component(self):
        baseline = build_optuna_study_name(
            model_family="paste-volume-resnet-small-v1",
            dataset_fingerprint="sha256:" + "a" * 64,
            search_config={"lr": [1e-5, 1e-3]},
        )

        assert baseline != build_optuna_study_name(
            model_family="another-model",
            dataset_fingerprint="sha256:" + "a" * 64,
            search_config={"lr": [1e-5, 1e-3]},
        )
        assert baseline != build_optuna_study_name(
            model_family="paste-volume-resnet-small-v1",
            dataset_fingerprint="sha256:" + "b" * 64,
            search_config={"lr": [1e-5, 1e-3]},
        )
        assert baseline != build_optuna_study_name(
            model_family="paste-volume-resnet-small-v1",
            dataset_fingerprint="sha256:" + "a" * 64,
            search_config={"lr": [1e-6, 1e-2]},
        )


class TestStorageContract:
    def test_redacts_server_credentials(self):
        uri = (
            "postgresql://alice:secret@db.example:5432/optuna"
            "?sslmode=require#credential-fragment"
        )

        redacted = redact_storage_uri(uri)

        assert "alice" not in redacted
        assert "secret" not in redacted
        assert "sslmode" not in redacted
        assert "credential-fragment" not in redacted
        assert redacted == "postgresql://db.example:5432/optuna"

    def test_requires_absolute_sqlite_path(self):
        with pytest.raises(ValueError, match="absolute"):
            validate_optuna_storage("sqlite:///relative.db")

    def test_trial_overrides_inject_number_and_only_redacted_storage(self, tmp_path):
        overrides = build_trial_metadata_overrides(
            study_name="study",
            trial_number=7,
            search_fingerprint="sha256:abc",
            storage_uri="postgresql://alice:secret@db.example/optuna",
            checkpoint_directory=tmp_path / "trial-7",
        )
        joined = " ".join(overrides)

        assert "hpo.trial_number=7" in overrides
        assert "secret" not in joined
        assert "alice" not in joined
        assert str((tmp_path / "trial-7").resolve()) in joined


class TestPersistentResults:
    def test_sqlite_study_resumes_and_serializes_every_trial(self, tmp_path):
        storage = f"sqlite:///{tmp_path / 'study.db'}"
        study = optuna.create_study(
            study_name="persistent",
            storage=storage,
            direction="minimize",
            load_if_exists=True,
        )
        study.optimize(lambda trial: trial.suggest_float("x", 0.0, 1.0), n_trials=2)
        resumed = optuna.create_study(
            study_name="persistent",
            storage=storage,
            direction="minimize",
            load_if_exists=True,
        )
        resumed.optimize(lambda trial: trial.suggest_float("x", 0.0, 1.0), n_trials=1)

        trials = optuna_trials_payload(
            resumed, run_ids_by_trial={0: "run-zero", 2: "run-two"}
        )
        path = write_optimization_results(
            tmp_path / "optimization_results.yaml",
            study_name="persistent",
            storage_uri=storage,
            direction="minimize",
            trials=trials,
        )
        report = yaml.safe_load(path.read_text(encoding="utf-8"))

        assert len(resumed.trials) == 3
        assert [trial["number"] for trial in report["trials"]] == [0, 1, 2]
        assert report["trials"][0]["mlflow_run_id"] == "run-zero"
        assert report["trials"][1]["mlflow_run_id"] is None
        assert report["storage_uri"] == storage


class TestHydraSweeperConfig:
    def test_packaged_hpo_config_instantiates_custom_sweeper(self, tmp_path):
        with initialize_config_module(
            config_module="pcbasm.pasting.paste_volume.conf", version_base="1.3"
        ):
            config = compose(
                config_name="train",
                overrides=[
                    "data.manifest=/tmp/composite.json",
                    "hparams_search=base_optuna",
                    f"hydra.sweeper.storage=sqlite:///{tmp_path / 'study.db'}",
                ],
                return_hydra_config=True,
            )

        sweeper = instantiate(config.hydra.sweeper)

        assert isinstance(sweeper, PersistentOptunaSweeper)
        assert config.trainer.max_epochs == 60
        assert config.trainer.early_stopping_patience == 10
        assert OmegaConf.select(config, "hydra.sweeper.n_jobs") == 1

    def test_real_multirun_reuses_persistent_study_and_links_mlflow_runs(
        self, tmp_path
    ):
        dataset = tmp_path / "dataset"
        dataset.mkdir()
        for index in range(3):
            write_synthetic_session(
                dataset,
                f"session-{index}",
                machine_id=f"machine-{index}",
                pad_count=1,
                width=32,
                height=32,
            )
        process, tracking_uri = _start_mlflow_server(tmp_path / "mlflow-server")
        storage = f"sqlite:///{tmp_path / 'study.db'}"
        sweep_directory = tmp_path / "sweep"
        command = (
            sys.executable,
            "-m",
            "pcbasm.pasting.paste_volume.train",
            "--multirun",
            "data.manifest=null",
            f"data.roots=[{dataset}]",
            "hparams_search=base_optuna",
            f"hydra.sweeper.storage={storage}",
            "hydra.sweeper.n_trials=1",
            f"hydra.sweep.dir={sweep_directory}",
            f"logger.tracking_uri={tracking_uri}",
            "logger.experiment_name=hpo-integration",
            "trainer.max_epochs=1",
            "trainer.max_steps=1",
            "trainer.compile_enabled=false",
            "trainer.deterministic=true",
            "data.augmentation.enabled=false",
            "data.constraints.max_size=64",
            "data.constraints.max_pixels=4096",
            "data.max_batch_pixels=8192",
            "data.max_batch_size=2",
            "model.stem_channels=[4,4,4]",
            "model.stage_channels=[4,4,4]",
            "model.blocks_per_stage=[1,1,1]",
            "model.group_norm_groups=1",
            "model.hidden_features=4",
        )
        try:
            first = subprocess.run(
                command,
                cwd=Path.cwd(),
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert first.returncode == 0, first.stdout + "\n" + first.stderr
            second = subprocess.run(
                command,
                cwd=Path.cwd(),
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert second.returncode == 0, second.stdout + "\n" + second.stderr

            summaries = list(sweep_directory.glob("optimization_results.yaml"))
            assert len(summaries) == 1
            report = yaml.safe_load(summaries[0].read_text(encoding="utf-8"))
            study = optuna.load_study(study_name=report["study_name"], storage=storage)
            assert len(study.trials) == 1
            assert study.trials[0].state == optuna.trial.TrialState.COMPLETE
            assert report["trials"][0]["mlflow_run_id"]
            run_id = report["trials"][0]["mlflow_run_id"]
            client = MlflowClient(tracking_uri=tracking_uri)
            run = client.get_run(run_id)
            assert run.info.status == "FINISHED"
            assert run.data.tags["hpo.study_name"] == report["study_name"]
            assert run.data.tags["hpo.trial_number"] == "0"
            assert any(
                artifact.path == "hpo/optimization_results.yaml"
                for artifact in client.list_artifacts(run_id, "hpo")
            )
        finally:
            process.terminate()
            process.wait(timeout=10)
