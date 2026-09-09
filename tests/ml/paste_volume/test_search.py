"""探索 entrypoint の公開契約.

Storage は実 sqlite file を使う。

Optuna をモックすると「複数プロセスが 1 個の study を共有する」という
この機構の唯一の目的が検査から消える。別プロセスから読めることは、実際に
別の interpreter を起こして確かめる。

trial の学習は本物の ``run_training`` を通す。

探索した値が ``ConfigComposition`` の上書きへ素通しされ、単発 run と同じ設定
経路を通ることがこの entrypoint の要なので、そこを迂回すると検査が空になる。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ml.data.image import ImageConstraints
from ml.paste_volume.experiment import compose_experiment
from ml.paste_volume.index import PasteVolumeSampleIndex
from ml.paste_volume.model import MODEL_FAMILY
from ml.paste_volume.search import main, run_search
from ml.serialization import make_strict_converter
from ml.tuning.runner import HyperparameterSearch
from ml.tuning.study import StudyIdentity, StudyResults, StudyStorage
from tests.ml.helpers import PROJECT_ROOT
from tests.ml.paste_volume.helpers import write_synthetic_sessions

SESSION_COUNT = 3
TRIAL_COUNT = 2

# 探索する 4 本のうち trainer.gradient_accumulation と model.group_norm_groups は
# 学習経路そのものを変えるので、trial ごとに実際に別の設定で学習が起きる。
SEARCH_SPACE_SIZE = 4


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("paste-volume-search-dataset")
    write_synthetic_sessions(root, session_count=SESSION_COUNT)
    return root


def _arguments(dataset_root: Path, workspace: Path) -> list[str]:
    """探索 1 プロセス分の argv.

    ``experiment=search`` が HPO trial の 60 epoch / patience 10 を持つが、
    テストでは 1 epoch へ上書きする（group 層より後の上書きが勝つ）。
    """

    return [
        "experiment=search",
        "logger=mlflow",
        "hyperparameter_search=base_optuna",
        f'data.roots=["{dataset_root}"]',
        "data.held_out_session=session-0",
        "data.max_batch_size=4",
        f"logger.tracking_uri=sqlite:///{workspace}/mlflow.db",
        f"logger.artifact_location={workspace}/mlartifacts",
        f"hyperparameter_search.storage_uri=sqlite:///{workspace}/optuna.db",
        f"hyperparameter_search.trial_count={TRIAL_COUNT}",
        f"hyperparameter_search.results_path={workspace}/study.json",
        f"run_directory={workspace}/runs",
        "trainer.max_epochs=1",
        "trainer.compile_enabled=false",
    ]


@pytest.fixture(scope="module")
def searched(
    dataset_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> tuple[StudyResults, Path]:
    """実 sqlite storage へ 2 trial 積む。module 内で使い回す."""

    workspace = tmp_path_factory.mktemp("paste-volume-search")
    results, error = run_search(_arguments(dataset_root, workspace))

    assert error is None, error
    assert results is not None
    return results, workspace


def _reader(storage_uri: str, study_name: str) -> dict[str, object]:
    """別プロセスから同じ storage を読む.

    「1 個の RDB study を複数の OS プロセスが共有する」ことが並列化の実体なので、
    同一プロセス内で読み直しても機構の検査にならない。
    """

    code = (
        "import json, optuna;"
        f"study = optuna.load_study(study_name={study_name!r},"
        f" storage={storage_uri!r});"
        "print(json.dumps({"
        "'trials': len(study.trials),"
        "'complete': sum(1 for t in study.trials if t.state.name == 'COMPLETE'),"
        "'parameters': sorted({name for t in study.trials for name in t.params}),"
        "}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=PROJECT_ROOT,
    )
    return json.loads(completed.stdout)


class TestHyperparameterSearch:
    """1 プロセス分の探索."""

    def test_it_completes_every_requested_trial(
        self, searched: tuple[StudyResults, Path]
    ):
        results, _ = searched

        assert results.completed_trial_count == TRIAL_COUNT
        assert len(results.trials) == TRIAL_COUNT

    def test_the_study_name_is_content_addressed(
        self, searched: tuple[StudyResults, Path]
    ):
        """Study 名が model family と dataset と探索空間から決まること."""

        results, _ = searched

        assert results.study_name.startswith(MODEL_FAMILY)
        assert results.search_space_fingerprint.startswith("sha256:")

    def test_every_trial_names_its_experiment_run(
        self, searched: tuple[StudyResults, Path]
    ):
        """完走した trial から実験 run を辿れること.

        紐付けが無いと「どの run がその値を出したか」を後から追えない。
        """

        results, _ = searched

        assert results.verify_lineage() is None
        assert all(trial.experiment_run_id for trial in results.trials)

    def test_each_trial_gets_its_own_run_directory(
        self, searched: tuple[StudyResults, Path]
    ):
        """Trial ごとに run directory を分けること.

        共有すると split.json も checkpoint も trial 間で上書きし合う。
        """

        results, workspace = searched
        directories = sorted(
            child.name for child in (workspace / "runs").iterdir() if child.is_dir()
        )

        assert len(directories) == TRIAL_COUNT
        assert all(name.startswith(results.study_name) for name in directories)
        for name in directories:
            assert (workspace / "runs" / name / "best.pt").is_file()

    def test_the_searched_values_reach_the_training_configuration(
        self, searched: tuple[StudyResults, Path]
    ):
        """探索した値が設定の dotted path そのままで渡ること.

        ``ConfigComposition`` の上書きへ素通しできる形でなければ、探索 run と
        単発 run が別の設定経路を通る。
        """

        results, _ = searched
        names = {name for trial in results.trials for name in trial.parameters}

        assert names == {
            "model.group_norm_groups",
            "trainer.gradient_accumulation",
            "trainer.learning_rate",
            "trainer.weight_decay",
        }
        assert len(names) == SEARCH_SPACE_SIZE

    def test_it_writes_the_results_document(self, searched: tuple[StudyResults, Path]):
        results, workspace = searched

        loaded, error = StudyResults.load(
            workspace / "study.json", converter=make_strict_converter()
        )

        assert error is None, error
        assert loaded == results

    def test_the_document_keeps_the_storage_uri_redacted(
        self, searched: tuple[StudyResults, Path]
    ):
        """成果物へ生の storage URI を残さないこと."""

        results, workspace = searched

        assert results.storage_uri_redacted.startswith("sqlite://")
        assert str(workspace) in results.storage_uri_redacted


class TestSharedStorage:
    """同じ storage へ別プロセスから合流できること."""

    def test_another_process_reads_the_same_trials(
        self, searched: tuple[StudyResults, Path]
    ):
        results, workspace = searched

        observed = _reader(f"sqlite:///{workspace}/optuna.db", results.study_name)

        assert observed["trials"] == TRIAL_COUNT
        assert observed["complete"] == TRIAL_COUNT
        assert observed["parameters"] == sorted(
            {name for trial in results.trials for name in trial.parameters}
        )

    def test_the_storage_is_a_file_on_disk(self, searched: tuple[StudyResults, Path]):
        """In-memory ではなく共有できる storage を使っていること.

        上のプロセス間の検査は、storage が実 file でなければ成立しない。
        """

        _, workspace = searched

        assert (workspace / "optuna.db").is_file()

    def test_a_second_process_adds_trials_to_the_same_study(
        self, dataset_root: Path, searched: tuple[StudyResults, Path]
    ):
        """同じ argv で起こしたもう 1 本の探索が trial を積み増すこと.

        ``trial_count`` は残 trial 数へ減算せず常に積み増す。何プロセスが
        合流するかを runner は知らない。
        """

        results, workspace = searched

        joined, error = run_search(
            [
                *_arguments(dataset_root, workspace),
                "hyperparameter_search.trial_count=1",
                f"run_directory={workspace}/joined",
            ]
        )

        assert error is None, error
        assert joined is not None
        assert joined.study_name == results.study_name
        assert joined.completed_trial_count == TRIAL_COUNT + 1

    def test_collect_reads_the_study_without_running_a_trial(
        self, dataset_root: Path, searched: tuple[StudyResults, Path]
    ):
        """Trial を回さずに、共有 storage の結果だけを読み戻せること.

        同一性は内容から決まるので、study 名を渡さなくても同じ study へ届く。
        """

        results, workspace = searched
        search = _search_of(dataset_root, workspace)

        collected, error = search.collect()

        assert error is None, error
        assert collected is not None
        assert collected.study_name == results.study_name
        assert collected.completed_trial_count >= TRIAL_COUNT


class TestSearchArguments:
    """Argv の不備を理由文字列で返すこと."""

    def test_it_refuses_a_search_without_a_space(
        self, dataset_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = main(
            [
                "experiment=search",
                "logger=mlflow",
                f'data.roots=["{dataset_root}"]',
                "data.held_out_session=session-0",
                f"logger.tracking_uri=sqlite:///{tmp_path}/mlflow.db",
                f"logger.artifact_location={tmp_path}/mlartifacts",
                f"run_directory={tmp_path}/runs",
            ]
        )

        assert code == 1
        assert "hyperparameter_search" in capsys.readouterr().err

    def test_it_refuses_a_relative_sqlite_storage(
        self, dataset_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Sqlite は絶対 path を要求すること.

        相対 path だと起動した directory ごとに study が分かれ、合流できない。
        """

        code = main(
            [
                "experiment=search",
                "logger=mlflow",
                "hyperparameter_search=base_optuna",
                f'data.roots=["{dataset_root}"]',
                "data.held_out_session=session-0",
                f"logger.tracking_uri=sqlite:///{tmp_path}/mlflow.db",
                f"logger.artifact_location={tmp_path}/mlartifacts",
                "hyperparameter_search.storage_uri=sqlite:///optuna.db",
                f"run_directory={tmp_path}/runs",
            ]
        )

        assert code == 1
        assert "絶対パス" in capsys.readouterr().err


def _search_of(dataset_root: Path, workspace: Path) -> HyperparameterSearch:
    """探索側と同じ材料から study の同一性を組み直す.

    ``run_search`` の中で作る identity と同じものを、公開している値だけから
    復元できることも同時に見る。
    """

    config, error = compose_experiment(_arguments(dataset_root, workspace))
    assert error is None, error
    assert config is not None
    search_config = config.hyperparameter_search
    assert search_config is not None
    index, error = PasteVolumeSampleIndex.from_roots(
        config.data.roots, constraints=ImageConstraints()
    )
    assert error is None, error
    assert index is not None
    identity, error = StudyIdentity.build(
        model_family=MODEL_FAMILY,
        dataset_fingerprint=index.dataset_fingerprint,
        search_space_fingerprint=search_config.search_space.fingerprint,
    )
    assert error is None, error
    assert identity is not None
    return HyperparameterSearch(
        identity=identity,
        storage=StudyStorage(uri=search_config.storage_uri),
        search_space=search_config.search_space,
    )
