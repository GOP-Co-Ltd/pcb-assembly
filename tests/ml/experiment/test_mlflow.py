"""実 MLflow tracking server に対する ``MLflowExperimentLogger`` の公開契約.

MLflow は 3rd-party 表面なのでモックしない。

このファイルに閉じた session fixture で local server を 1 度だけ起動し、
記録結果は ``MlflowClient`` で読み戻して照合する。

CI は ``--all-groups`` で mlflow を必ず install するため、import できないことを
理由に skip してはならない。

skip してよいのは port を bind できない等の実環境失敗だけで、理由を必ず明示する。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import mlflow
import pytest
from mlflow.exceptions import MlflowException

from ml.experiment.mlflow import MLflowExperimentLogger, MLflowRunTarget

SERVER_STARTUP_TIMEOUT_SECONDS = 60.0
SERVER_POLL_INTERVAL_SECONDS = 0.5
SERVER_SHUTDOWN_TIMEOUT_SECONDS = 15.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _health_reached(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False


@pytest.fixture(scope="session")
def tracking_uri(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Local の MLflow tracking server を 1 度だけ起動する."""

    store = tmp_path_factory.mktemp("mlflow-store")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mlflow",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--backend-store-uri",
            f"sqlite:///{store / 'mlflow.db'}",
            "--artifacts-destination",
            str(store / "artifacts"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + SERVER_STARTUP_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.skip(
                    "MLflow server が起動直後に終了しました "
                    f"(exit code {process.returncode}、port {port})"
                )
            if _health_reached(f"{url}/health"):
                break
            time.sleep(SERVER_POLL_INTERVAL_SECONDS)
        else:
            pytest.skip(
                f"MLflow server が {SERVER_STARTUP_TIMEOUT_SECONDS} 秒以内に "
                f"{url}/health へ応答しませんでした"
            )
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=SERVER_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=SERVER_SHUTDOWN_TIMEOUT_SECONDS)


@pytest.fixture
def client(tracking_uri: str) -> mlflow.MlflowClient:
    """起動済み server を見る MLflow client."""

    return mlflow.MlflowClient(tracking_uri=tracking_uri)


def _unique_experiment_name() -> str:
    return f"ml-mr4-{uuid.uuid4().hex[:12]}"


def _target(tracking_uri: str, **overrides: object) -> MLflowRunTarget:
    values: dict[str, object] = {
        "tracking_uri": tracking_uri,
        "experiment_name": _unique_experiment_name(),
    }
    values.update(overrides)
    return MLflowRunTarget(**values)  # pyright: ignore[reportArgumentType]


class TestMLflowRunTarget:
    """Run の宛先設定は使う前に理由つきで検証できる."""

    def test_valid_target_has_no_reason(self):
        target = MLflowRunTarget(
            tracking_uri="http://127.0.0.1:5000", experiment_name="experiment"
        )

        assert target.validate() is None

    def test_empty_tracking_uri_is_rejected(self):
        target = MLflowRunTarget(tracking_uri="", experiment_name="experiment")

        reason = target.validate()

        assert reason is not None
        assert "tracking_uri" in reason

    def test_empty_experiment_name_is_rejected(self):
        target = MLflowRunTarget(
            tracking_uri="http://127.0.0.1:5000", experiment_name=""
        )

        reason = target.validate()

        assert reason is not None
        assert "experiment_name" in reason

    @pytest.mark.parametrize(
        "tracking_uri",
        ["file:///abs/mlruns", "./mlruns", "/abs/mlruns"],
        ids=["file-uri", "relative-path", "absolute-path"],
    )
    def test_a_filesystem_tracking_uri_is_rejected(self, tracking_uri: str):
        """MLflow が store を作れない記録先は、logger を組む前に落とす.

        MLflow 3.15 の filesystem backend は maintenance mode で、``start`` の
        内側で ``MlflowException`` になる（下の契約ピンが実測する）。
        """

        target = MLflowRunTarget(
            tracking_uri=tracking_uri, experiment_name="experiment"
        )

        reason = target.validate()

        assert reason is not None
        assert "MLFLOW_ALLOW_FILE_STORE" in reason

    def test_a_database_tracking_uri_is_accepted(self):
        """上の拒否が「scheme を問わず落とす」形へ退化していないこと."""

        target = MLflowRunTarget(
            tracking_uri="sqlite:////abs/mlflow.db", experiment_name="experiment"
        )

        assert target.validate() is None

    def test_sanitized_tracking_uri_drops_credentials(self):
        target = MLflowRunTarget(
            tracking_uri="http://user:secret@127.0.0.1:5000?token=abc",
            experiment_name="experiment",
        )

        assert target.sanitized_tracking_uri == "http://127.0.0.1:5000"


class TestFileStoreContract:
    """拒否の根拠になっている MLflow 側の振る舞いを実測で固定する.

    ``file://`` を拒む理由は「MLflow が受け付けないから」であって、こちらの
    好みではない。MLflow が filesystem backend を復帰させたらこの検査が落ちて、
    拒否そのものを見直せる。
    """

    def test_the_filesystem_backend_refuses_to_open(self, tmp_path: Path):
        previous = mlflow.get_tracking_uri()
        try:
            mlflow.set_tracking_uri(f"file://{tmp_path / 'mlruns'}")

            with pytest.raises(MlflowException) as exception:
                mlflow.set_experiment("filesystem-backend")

            assert "maintenance mode" in str(exception.value)
        finally:
            mlflow.set_tracking_uri(previous)


class TestExistingExperimentArtifactLocation:
    """成果物の置き場所は experiment を作るときにしか決められない.

    Database backend の store で実測する。置き場所を焼き付けるのが experiment の
    作成時だという事実そのものが、この検査の対象。
    """

    @pytest.fixture
    def database_uri(self, tmp_path: Path) -> Iterator[str]:
        """この test だけの sqlite tracking store.

        ``mlflow.set_tracking_uri`` はプロセス大域なので、抜けるときに戻す。
        """

        previous = mlflow.get_tracking_uri()
        try:
            yield f"sqlite:///{tmp_path / 'mlflow.db'}"
        finally:
            mlflow.set_tracking_uri(previous)

    def _run(self, database_uri: str, name: str, location: Path) -> str:
        logger = MLflowExperimentLogger(
            MLflowRunTarget(
                tracking_uri=database_uri,
                experiment_name=name,
                artifact_location=str(location),
            )
        )
        run_id = logger.start(run_kind="training")
        logger.end()
        return run_id

    def test_it_refuses_an_experiment_created_for_another_location(
        self, database_uri: str, tmp_path: Path
    ):
        """要求と違う場所へ黙って書かないこと.

        既存 experiment には ``artifact_location`` が効かないので、通すと 5 fold
        ぜんぶの成果物が起動 directory の ``./mlruns`` へ落ちる。
        """

        name = _unique_experiment_name()
        self._run(database_uri, name, tmp_path / "first")
        logger = MLflowExperimentLogger(
            MLflowRunTarget(
                tracking_uri=database_uri,
                experiment_name=name,
                artifact_location=str(tmp_path / "second"),
            )
        )

        with pytest.raises(ValueError) as exception:
            logger.start(run_kind="training")

        assert "artifact_location" in str(exception.value)
        assert str(tmp_path / "second") in str(exception.value)

    def test_the_same_location_keeps_working(self, database_uri: str, tmp_path: Path):
        """同じ場所を指す 2 本目は通ること.

        上の拒否が「既存 experiment は常に駄目」へ退化していないか。5 fold は 1 つの experiment
        を共有するので、退化すると 2 fold 目が動かない。
        """

        name = _unique_experiment_name()
        location = tmp_path / "shared"
        self._run(database_uri, name, location)

        second = self._run(database_uri, name, location)

        client = mlflow.MlflowClient(tracking_uri=database_uri)
        artifact_uri = client.get_run(second).info.artifact_uri
        assert artifact_uri is not None
        assert artifact_uri.startswith(str(location))

    def test_the_artifacts_land_under_the_requested_location(
        self, database_uri: str, tmp_path: Path
    ):
        """置き場所の指定そのものが効いていること."""

        location = tmp_path / "requested"

        run_id = self._run(database_uri, _unique_experiment_name(), location)

        client = mlflow.MlflowClient(tracking_uri=database_uri)
        artifact_uri = client.get_run(run_id).info.artifact_uri
        assert artifact_uri is not None
        assert artifact_uri.startswith(str(location))


class TestMLflowExperimentLoggerLifecycle:
    """Run 作成から終了までを実 server へ記録し、読み戻して照合する."""

    def test_invalid_target_is_rejected_at_construction(self):
        with pytest.raises(ValueError) as exception:
            MLflowExperimentLogger(
                MLflowRunTarget(tracking_uri="", experiment_name="experiment")
            )

        assert "tracking_uri" in str(exception.value)

    def test_full_run_is_recorded(
        self, tracking_uri: str, client: mlflow.MlflowClient, tmp_path: Path
    ):
        artifact = tmp_path / "checkpoint-note.txt"
        artifact.write_text("recorded", encoding="utf-8")
        logger = MLflowExperimentLogger(_target(tracking_uri))

        run_id = logger.start(run_kind="training", run_name="mr4-run")
        logger.log_params({"max_epochs": 2, "deterministic": True})
        logger.log_metrics({"validation/loss": 0.25}, step=3)
        logger.set_tags({"git.commit": "abc123"})
        logger.log_artifact(artifact, artifact_path="notes")
        logger.flush()
        logger.end(status="FINISHED")

        recorded = client.get_run(run_id)
        assert recorded.info.status == "FINISHED"
        assert recorded.data.params["max_epochs"] == "2"
        assert recorded.data.params["deterministic"] == "True"
        assert recorded.data.metrics["validation/loss"] == pytest.approx(0.25)
        assert recorded.data.tags["git.commit"] == "abc123"
        # run_kind をどのタグ名で持つかは実装に委ねるが、記録されること自体は契約
        assert "training" in recorded.data.tags.values()
        assert [item.path for item in client.list_artifacts(run_id, "notes")] == [
            "notes/checkpoint-note.txt"
        ]

    def test_metric_step_axis_is_preserved(
        self, tracking_uri: str, client: mlflow.MlflowClient
    ):
        logger = MLflowExperimentLogger(_target(tracking_uri))

        run_id = logger.start(run_kind="training")
        logger.log_metrics({"validation/loss": 0.5}, step=10)
        logger.log_metrics({"validation/loss": 0.4}, step=20)
        logger.end()

        history = client.get_metric_history(run_id, "validation/loss")
        assert sorted((point.step, point.value) for point in history) == [
            (10, 0.5),
            (20, 0.4),
        ]

    def test_start_tags_are_recorded(
        self, tracking_uri: str, client: mlflow.MlflowClient
    ):
        logger = MLflowExperimentLogger(_target(tracking_uri))

        run_id = logger.start(run_kind="training", tags={"stage": "smoke"})
        logger.end()

        assert client.get_run(run_id).data.tags["stage"] == "smoke"

    def test_failed_status_is_recorded(
        self, tracking_uri: str, client: mlflow.MlflowClient
    ):
        logger = MLflowExperimentLogger(_target(tracking_uri))

        run_id = logger.start(run_kind="training")
        logger.end(status="FAILED")

        assert client.get_run(run_id).info.status == "FAILED"

    def test_resume_appends_to_the_same_run(
        self, tracking_uri: str, client: mlflow.MlflowClient
    ):
        experiment_name = _unique_experiment_name()
        first = MLflowExperimentLogger(
            MLflowRunTarget(tracking_uri=tracking_uri, experiment_name=experiment_name)
        )
        run_id = first.start(run_kind="training")
        first.log_metrics({"validation/loss": 0.5}, step=1)
        first.end()

        resumed = MLflowExperimentLogger(
            MLflowRunTarget(
                tracking_uri=tracking_uri,
                experiment_name=experiment_name,
                resume_run_id=run_id,
            )
        )
        resumed_run_id = resumed.start(run_kind="training")
        resumed.log_metrics({"validation/loss": 0.3}, step=2)
        resumed.end()

        assert resumed_run_id == run_id
        history = client.get_metric_history(run_id, "validation/loss")
        assert sorted(point.step for point in history) == [1, 2]


class TestMLflowExperimentLoggerRejections:
    """Run の状態に合わない操作は理由つきで拒否する."""

    def test_run_id_before_start_is_rejected(self, tracking_uri: str):
        logger = MLflowExperimentLogger(_target(tracking_uri))

        with pytest.raises(RuntimeError) as exception:
            _ = logger.run_id

        assert "start" in str(exception.value)

    def test_log_params_before_start_is_rejected(self, tracking_uri: str):
        logger = MLflowExperimentLogger(_target(tracking_uri))

        with pytest.raises(RuntimeError) as exception:
            logger.log_params({"max_epochs": 2})

        assert "start" in str(exception.value)

    def test_second_start_is_rejected(self, tracking_uri: str):
        logger = MLflowExperimentLogger(_target(tracking_uri))
        logger.start(run_kind="training")

        with pytest.raises(RuntimeError) as exception:
            logger.start(run_kind="training")

        assert "start" in str(exception.value)
        logger.end()

    def test_operations_after_end_are_rejected(self, tracking_uri: str):
        logger = MLflowExperimentLogger(_target(tracking_uri))
        logger.start(run_kind="training")
        logger.end()

        with pytest.raises(RuntimeError) as exception:
            logger.log_metrics({"validation/loss": 0.1}, step=1)

        assert "end" in str(exception.value)

    def test_missing_artifact_file_is_rejected(self, tracking_uri: str, tmp_path: Path):
        logger = MLflowExperimentLogger(_target(tracking_uri))
        logger.start(run_kind="training")

        with pytest.raises(FileNotFoundError):
            logger.log_artifact(tmp_path / "absent.txt")

        logger.end()
