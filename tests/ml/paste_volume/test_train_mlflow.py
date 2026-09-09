"""学習 entrypoint と実 MLflow の統合（Phase 3）.

MLflow は 3rd-party 表面なのでモックしない。

記録した内容は ``MlflowClient`` で読み戻して照合する。書いた側の記憶では
なく、store に残ったものを見る。

``tests/ml/experiment/test_mlflow.py`` は http server を立てるが、ここは
local sqlite の tracking store で足りる。Phase 3 の要件は「実 local MLflow」
であって server 常駐ではない。

**file store は使わない。** MLflow 3.15 の filesystem backend は maintenance
mode で、``MLFLOW_ALLOW_FILE_STORE=true`` で opt out しない限り store を作る
時点で例外になる。仕様書 §4 が挙げる「local SQLite から shared server へ同じ
client API で移行できる」形をそのまま使う。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import mlflow
import pytest
from mlflow.tracking import MlflowClient

from ml.paste_volume.experiment import CONFIG_FILE_NAME, SPLIT_FILE_NAME
from ml.paste_volume.train import main
from tests.ml.paste_volume.helpers import write_synthetic_sessions

SESSION_COUNT = 3
HELD_OUT = "session-1"
MAX_EPOCHS = 2
EXPERIMENT_NAME = "paste-volume-test"


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Dataset・tracking store・run directory を 1 か所へ集める.

    ``mlflow.set_tracking_uri`` はプロセス大域なので、この module を出るときに
    既定へ戻す。
    """

    root = tmp_path_factory.mktemp("paste-volume-mlflow")
    write_synthetic_sessions(root / "data", session_count=SESSION_COUNT)
    previous = mlflow.get_tracking_uri()
    try:
        yield root
    finally:
        mlflow.set_tracking_uri(previous)


@pytest.fixture(scope="module")
def tracking_uri(workspace: Path) -> str:
    return f"sqlite:///{workspace}/mlflow.db"


@pytest.fixture(scope="module")
def trained(workspace: Path, tracking_uri: str) -> str:
    """Argv から 1 fold を学習し、その run ID を返す."""

    code = main(
        [
            "experiment=base",
            "logger=mlflow",
            f'data.roots=["{workspace / "data"}"]',
            f"data.held_out_session={HELD_OUT}",
            "data.max_batch_size=4",
            f"logger.tracking_uri={tracking_uri}",
            f"logger.experiment_name={EXPERIMENT_NAME}",
            f"logger.artifact_location={workspace}/mlartifacts",
            f"trainer.max_epochs={MAX_EPOCHS}",
            "trainer.compile_enabled=false",
            f"run_directory={workspace}/runs/{HELD_OUT}",
        ]
    )

    assert code == 0
    client = MlflowClient(tracking_uri=tracking_uri)
    experiment = client.get_experiment_by_name(EXPERIMENT_NAME)
    assert experiment is not None
    runs = client.search_runs([experiment.experiment_id])
    assert len(runs) == 1
    return str(runs[0].info.run_id)


@pytest.fixture
def client(tracking_uri: str) -> MlflowClient:
    return MlflowClient(tracking_uri=tracking_uri)


class TestRunRecord:
    """1 run が store に残す tag / param / metric / artifact."""

    def test_the_run_finished(self, client: MlflowClient, trained: str):
        run = client.get_run(trained)

        assert run.info.status == "FINISHED"

    def test_it_tags_the_run_kind_and_the_identities(
        self, client: MlflowClient, trained: str
    ):
        """仕様書 §4 の tag（run_kind・由来・dataset / split の同一性）."""

        tags = client.get_run(trained).data.tags

        assert tags["run_kind"] == "base-train"
        assert tags["split.dimension"] == "session"
        assert tags["split.held_out_session"] == HELD_OUT
        assert tags["dataset.fingerprint"].startswith("sha256:")
        assert tags["split.fingerprint"].startswith("sha256:")
        assert tags["model.family"] == "paste-volume-resnet-small-v1"

    def test_it_records_the_model_size_and_the_dependencies_as_params(
        self, client: MlflowClient, trained: str
    ):
        """仕様書 §4 の param（model 構成・parameter 数・GMAC・version）.

        MLflow は param を文字列で返すので、値そのものを文字列と突き合わせる。
        """

        params = client.get_run(trained).data.params

        assert params["model.parameter_count"] == "395048"
        assert params["model.trainable_parameter_count"] == "395048"
        assert params["model.giga_multiply_accumulate"].startswith("1.3071")
        assert params["max_epochs"] == str(MAX_EPOCHS)
        assert params["monitor"] == "negative_log_likelihood"
        assert params["dependency.torch"] != ""
        assert params["dependency.mlflow"] != ""

    def test_it_records_one_point_per_epoch_for_train_and_validation(
        self, client: MlflowClient, trained: str
    ):
        """Epoch ごとの metric が train / validation の両方に残ること."""

        for key in (
            "train/negative_log_likelihood",
            "validation/negative_log_likelihood",
            "validation/mean_absolute_error",
            "learning_rate",
        ):
            history = client.get_metric_history(trained, key)

            assert len(history) == MAX_EPOCHS, key

    def test_it_records_the_saturation_diagnostic(
        self, client: MlflowClient, trained: str
    ):
        """平均 head の飽和と blank の観測点が epoch ごとに残ること.

        step 8 の健全性判定（``saturated_positive_fraction`` が最終 epoch で
        0.05 未満）を run 記録だけで確かめられるようにする。
        """

        for key in (
            "validation/saturated_positive_fraction",
            "validation/zero_target_exact_zero_fraction",
            "validation/one_standard_deviation_coverage",
        ):
            assert client.get_metric_history(trained, key) != [], key

    def test_it_uploads_the_resolved_configuration_and_the_split(
        self, client: MlflowClient, trained: str
    ):
        """解決済み config と split manifest が artifact に載ること."""

        names = {item.path for item in client.list_artifacts(trained)}

        assert {CONFIG_FILE_NAME, SPLIT_FILE_NAME} <= names

    def test_it_uploads_the_final_checkpoint(self, client: MlflowClient, trained: str):
        names = {item.path for item in client.list_artifacts(trained)}

        assert "final.pt" in names

    def test_the_artifacts_land_under_the_configured_location(
        self, client: MlflowClient, trained: str, workspace: Path
    ):
        """成果物が指定した場所に置かれること.

        database backend は experiment を作るときに置き場所を決めないと現在
        directory の下（``./mlruns``）を使う。起こした directory ごとに成果物が
        散ると、あとから run を開いても artifact を辿れない。
        """

        artifact_uri = client.get_run(trained).info.artifact_uri

        assert artifact_uri is not None
        assert artifact_uri.startswith(f"{workspace}/mlartifacts")
        assert not (Path.cwd() / "mlruns").exists()
