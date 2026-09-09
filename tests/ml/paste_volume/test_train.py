"""学習 entrypoint の公開契約.

観測するのは 4 つ。

1. run directory へ残る成果物と、記録先へ載る params / tags / artifact
2. 学習を始める前に落とす判定（計算量 gate、初期 weight のキー集合）
3. 中断して再開したときに、通し実行と同じ重みと metric へ到達すること
4. argv の不備を理由文字列で返すこと

3 は「一致する」型の検査なので、**中断が実際に起きたこと**を別に観測する。

中断が no-op でも一致は成立してしまう（``memory/negative-assertion-needs-self-check.md``）。
"""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
import torch
from torch import nn
from torch.nn.modules.module import register_module_forward_hook
from torch.utils.hooks import RemovableHandle

from ml.data.image import ImageConstraints
from ml.data.split import SplitManifest
from ml.model.multiview import MultiViewGaussianRegressor
from ml.paste_volume.experiment import (
    CALIBRATION_FILE_NAME,
    CONFIG_FILE_NAME,
    SPLIT_FILE_NAME,
    WEIGHTS_FILE_NAME,
    PasteVolumeExperimentConfig,
    compose_experiment,
    load_experiment_config,
)
from ml.paste_volume.index import PasteVolumeSampleIndex
from ml.paste_volume.train import (
    GIGA_MULTIPLY_ACCUMULATE_BUDGET,
    PARAMETER_BUDGET,
    ModelWeights,
    UncertaintyCalibration,
    load_model_weights,
    main,
    run_training,
    save_model_weights,
)
from ml.training.checkpoint import CheckpointRole, CheckpointStore
from ml.training.loop import TrainingOutcome
from tests.ml.paste_volume.helpers import write_synthetic_sessions
from tests.ml.support import RecordingExperimentLogger

DEVICE = torch.device("cpu")

# 合成 dataset の規模。session 3 本が leave-one-session-out の下限
# （held-out を除いた残りが 2 group 未満だと fold が立たない）。
SESSION_COUNT = 3
CELL_COUNT = 6

HELD_OUT = "session-0"

# 学習を数 epoch で終わらせるための共通の上書き。
#
# compile は既定 ON（仕様書 §3）だが、可変 shape で毎 batch 再 compile すると
# 1 epoch が数十秒になる。compile 経路そのものは test_task.py が測る。
FAST_ARGUMENTS = (
    "trainer.max_epochs=3",
    "trainer.compile_enabled=false",
    "data.max_batch_size=4",
)


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """合成 session 3 本。module 内で使い回す（読み出ししかしない）."""

    root = tmp_path_factory.mktemp("paste-volume-train-dataset")
    write_synthetic_sessions(root, session_count=SESSION_COUNT, cell_count=CELL_COUNT)
    return root


def _composed(
    dataset_root: Path, run_directory: Path, *overrides: str
) -> PasteVolumeExperimentConfig:
    config, error = compose_experiment(
        [
            "experiment=base",
            f'data.roots=["{dataset_root}"]',
            f"data.held_out_session={HELD_OUT}",
            *FAST_ARGUMENTS,
            f"run_directory={run_directory}",
            *overrides,
        ]
    )

    assert error is None, error
    assert config is not None
    return config


def _trained(
    dataset_root: Path, run_directory: Path, *overrides: str
) -> tuple[TrainingOutcome, RecordingExperimentLogger]:
    logger = RecordingExperimentLogger()
    outcome, error = run_training(
        _composed(dataset_root, run_directory, *overrides),
        logger=logger,
        device=DEVICE,
    )

    assert error is None, error
    assert outcome is not None
    return outcome, logger


def _sessions_of(index: PasteVolumeSampleIndex, sample_ids: Sequence[str]) -> set[str]:
    """Sample ID の集合が触れている session label を返す.

    「held-out が train / validation に現れない」型の検査に使う唯一の観測器。

    検査器そのものが働くことは、故意に混ぜた manifest で確かめる。
    """

    return {index.entry_for(sample_id).session_label for sample_id in sample_ids}


def _index(dataset_root: Path) -> PasteVolumeSampleIndex:
    index, error = PasteVolumeSampleIndex.from_roots(
        [dataset_root], constraints=ImageConstraints()
    )

    assert error is None, error
    assert index is not None
    return index


def _state_dict(
    store: CheckpointStore, role: CheckpointRole
) -> Mapping[str, torch.Tensor]:
    checkpoint, reason = store.load(role)

    assert reason is None, reason
    assert checkpoint is not None
    return checkpoint.model_state


def _assert_same_weights(
    actual: Mapping[str, torch.Tensor], expected: Mapping[str, torch.Tensor]
) -> None:
    assert sorted(actual) == sorted(expected)
    for name, tensor in expected.items():
        assert torch.equal(actual[name], tensor), name


@contextmanager
def _terminating_at(training_forward: int) -> Iterator[list[int]]:
    """指定回目の学習 forward で自分自身へ SIGTERM を送る.

    学習 loop の外側から、時間に依らず決定論的に中断を起こす。

    ``torch.nn`` の大域 forward hook を使うのは、``run_training`` が task も
    model も内側で組み立てるため。task を差し替える口を production 側へ
    開けると、その口は本番では誰も使わない。

    ``yield`` する list は学習 forward の回数で、中断が起きたかどうかを
    呼び出し側が観測する材料になる。
    """

    counted: list[int] = [0]

    def observe(module: nn.Module, inputs: object, output: object) -> None:
        if not isinstance(module, MultiViewGaussianRegressor) or not module.training:
            return
        counted[0] += 1
        if counted[0] == training_forward:
            os.kill(os.getpid(), signal.SIGTERM)

    handle: RemovableHandle = register_module_forward_hook(observe)
    try:
        yield counted
    finally:
        handle.remove()


class TestRunArtifacts:
    """1 run が run directory へ残すもの."""

    def test_it_writes_every_artifact_the_specification_lists(
        self, dataset_root: Path, tmp_path: Path
    ):
        """仕様書 §5 の run directory の中身が揃うこと."""

        run_directory = tmp_path / "run"

        _trained(dataset_root, run_directory)

        assert (run_directory / CONFIG_FILE_NAME).is_file()
        assert (run_directory / SPLIT_FILE_NAME).is_file()
        assert (run_directory / WEIGHTS_FILE_NAME).is_file()
        assert (run_directory / CALIBRATION_FILE_NAME).is_file()
        assert (run_directory / "best.pt").is_file()
        assert (run_directory / "latest.pt").is_file()
        assert (run_directory / "final.pt").is_file()

    def test_the_saved_config_is_the_resolved_one(
        self, dataset_root: Path, tmp_path: Path
    ):
        """config.json が argv ではなく解決済みの値を持つこと."""

        run_directory = tmp_path / "run"
        config = _composed(dataset_root, run_directory)
        logger = RecordingExperimentLogger()
        run_training(config, logger=logger, device=DEVICE)

        loaded, error = load_experiment_config(run_directory / CONFIG_FILE_NAME)

        assert error is None, error
        assert loaded == config

    def test_the_configuration_and_the_split_reach_the_experiment_record(
        self, dataset_root: Path, tmp_path: Path
    ):
        """解決済み config と split が記録先の artifact に載ること."""

        run_directory = tmp_path / "run"

        _, logger = _trained(dataset_root, run_directory)

        recorded = {path.name for path, _ in logger.artifacts}
        assert {CONFIG_FILE_NAME, SPLIT_FILE_NAME} <= recorded

    def test_the_weights_hold_the_best_model_state(
        self, dataset_root: Path, tmp_path: Path
    ):
        """weights.pt が best checkpoint と同じ重みを持つこと.

        Trainer は終了処理で best の重みを model へ読み戻すので、その後に書き出す weights.pt は
        best と一致する。final.pt との一致では「best を読み戻し忘れた」変異が見えない。
        """

        run_directory = tmp_path / "run"

        _trained(dataset_root, run_directory)

        weights, error = load_model_weights(run_directory / WEIGHTS_FILE_NAME)
        assert error is None, error
        assert weights is not None
        _assert_same_weights(
            weights.model_state, _state_dict(CheckpointStore(run_directory), "best")
        )

    def test_the_weights_carry_the_model_and_preprocess_schema(
        self, dataset_root: Path, tmp_path: Path
    ):
        """weights.pt が model 構成と前処理制約を同梱すること.

        checkpoint は model 構成を持たないので、これが無いと重みだけを受け取った側が形を組み直せない。
        """

        run_directory = tmp_path / "run"
        config = _composed(dataset_root, run_directory)
        _trained(dataset_root, run_directory)

        weights, error = load_model_weights(run_directory / WEIGHTS_FILE_NAME)

        assert error is None, error
        assert weights is not None
        assert weights.model_config == config.model
        assert weights.constraints == config.data.constraints
        assert weights.model_family == "paste-volume-resnet-small-v1"

    def test_it_rejects_weights_of_another_kind(self, tmp_path: Path):
        """Weights の読み込みが封筒を確かめること."""

        path = tmp_path / "weights.pt"
        torch.save({"kind": "something-else", "schema_version": 1}, path)

        weights, error = load_model_weights(path)

        assert weights is None
        assert error is not None
        assert "something-else" in error


class TestSplitIsolation:
    """Held-out session が学習側へ漏れないこと."""

    def test_the_held_out_session_stays_out_of_train_and_validation(
        self, dataset_root: Path, tmp_path: Path
    ):
        run_directory = tmp_path / "run"
        _trained(dataset_root, run_directory)
        index = _index(dataset_root)
        manifest, reason = SplitManifest.load(
            run_directory / SPLIT_FILE_NAME,
            dataset_fingerprint=index.dataset_fingerprint,
        )

        assert reason is None, reason
        assert manifest is not None
        assert _sessions_of(index, manifest.test_sample_ids) == {HELD_OUT}
        assert HELD_OUT not in _sessions_of(index, manifest.train_sample_ids)
        assert HELD_OUT not in _sessions_of(index, manifest.validation_sample_ids)

    def test_the_same_observation_finds_a_held_out_session_that_leaked(
        self, dataset_root: Path, tmp_path: Path
    ):
        """検査器が働くことを、故意に混ぜた split で確かめる.

        「現れない」型の assert なので、観測器が壊れると常に緑になる。
        """

        run_directory = tmp_path / "run"
        _trained(dataset_root, run_directory)
        index = _index(dataset_root)
        manifest, _ = SplitManifest.load(
            run_directory / SPLIT_FILE_NAME,
            dataset_fingerprint=index.dataset_fingerprint,
        )
        assert manifest is not None
        leaked = (*manifest.train_sample_ids, manifest.test_sample_ids[0])

        assert HELD_OUT in _sessions_of(index, leaked)

    def test_it_refuses_a_split_of_another_fold(
        self, dataset_root: Path, tmp_path: Path
    ):
        """同じ run directory を別の fold で使い回すと拒否されること.

        run directory を fold ごとに分けるのは運用の約束だが、約束だけだと要求した session が train
        に入ったまま run が進む。
        """

        run_directory = tmp_path / "run"
        _trained(dataset_root, run_directory)

        outcome, error = run_training(
            _composed(dataset_root, run_directory, "data.held_out_session=session-2"),
            logger=RecordingExperimentLogger(),
            device=DEVICE,
        )

        assert outcome is None
        assert error is not None
        assert "held_out_session" in error


class TestRecordedParameters:
    """記録先へ載る params が run の実測を映すこと."""

    def test_it_records_the_measured_model_size(
        self, dataset_root: Path, tmp_path: Path
    ):
        _, logger = _trained(dataset_root, tmp_path / "run")

        assert logger.params["model.parameter_count"] == 395_048
        assert logger.params["model.trainable_parameter_count"] == 395_048
        assert logger.params["model.giga_multiply_accumulate"] == pytest.approx(
            1.307149, abs=1e-6
        )

    def test_freezing_shows_up_in_the_trainable_parameter_count(
        self, dataset_root: Path, tmp_path: Path
    ):
        """Fine-tune の freeze を掛け忘れた run が記録から見えること.

        freeze を忘れても run は完走し、metric も成果物も何ひとつ変わらない。
        学習対象の要素数だけが唯一の観測点になる。
        """

        _, logger = _trained(dataset_root, tmp_path / "run", "model.fine_tune=true")

        assert logger.params["model.parameter_count"] == 395_048
        assert logger.params["model.trainable_parameter_count"] == 308_680
        assert logger.params["model.frozen_parameter_tensor_count"] == 21

    def test_it_records_the_train_split_scale_next_to_the_mean_bias(
        self, dataset_root: Path, tmp_path: Path
    ):
        """平均 bias の初期値を config 固定にした代わりの観測点（計画書 R5）."""

        _, logger = _trained(dataset_root, tmp_path / "run")

        assert logger.params["data.train_measured_mean_ul"] == pytest.approx(0.123)
        assert "model.mean_bias_deviation" not in logger.tags

    def test_it_reports_a_mean_bias_far_from_the_measured_scale(
        self, dataset_root: Path, tmp_path: Path
    ):
        """真値スケールから 3 倍以上外れたら理由をタグへ残すこと."""

        _, logger = _trained(
            dataset_root, tmp_path / "run", "model.mean_bias_initial=0.001"
        )

        assert "model.mean_bias_deviation" in logger.tags

    def test_it_records_the_provenance_and_the_split_identity(
        self, dataset_root: Path, tmp_path: Path
    ):
        _, logger = _trained(dataset_root, tmp_path / "run")

        assert logger.tags["split.dimension"] == "session"
        assert logger.tags["split.held_out_session"] == HELD_OUT
        assert logger.tags["dataset.fingerprint"].startswith("sha256:")
        assert logger.tags["split.fingerprint"].startswith("sha256:")
        assert "git.commit" in logger.tags or "git.unavailable" in logger.tags


class TestComputationBudget:
    """仕様書 §2 の parameter 数・計算量の上限."""

    def test_the_v1_encoder_passes_the_budget(self, dataset_root: Path, tmp_path: Path):
        """既定の構成が gate を通ること.

        通らない gate は下の 2 件を空虚にする（どんな model でも拒否される）。
        """

        outcome, _ = _trained(dataset_root, tmp_path / "run")

        assert outcome.epochs_completed > 0

    def test_it_refuses_a_model_over_the_parameter_budget(
        self, dataset_root: Path, tmp_path: Path
    ):
        run_directory = tmp_path / "run"

        outcome, error = run_training(
            _composed(dataset_root, run_directory, "model.stage_channels=[48, 512]"),
            logger=RecordingExperimentLogger(),
            device=DEVICE,
        )

        assert outcome is None
        assert error is not None
        assert str(PARAMETER_BUDGET) in error

    def test_it_refuses_a_model_over_the_computation_budget(
        self, dataset_root: Path, tmp_path: Path
    ):
        """Parameter 数が同じまま計算量だけ超える構成を拒否すること.

        stride を外すと parameter は 1 つも増えないので、parameter 側の判定では捕まらない。2
        つの上限が別々に効いていることが見える。
        """

        run_directory = tmp_path / "run"

        outcome, error = run_training(
            _composed(
                dataset_root,
                run_directory,
                "model.stem_strides=[1, 1]",
                "model.stage_strides=[1, 1]",
            ),
            logger=RecordingExperimentLogger(),
            device=DEVICE,
        )

        assert outcome is None
        assert error is not None
        assert f"{GIGA_MULTIPLY_ACCUMULATE_BUDGET} GMAC" in error

    def test_a_refused_model_does_not_start_the_run(
        self, dataset_root: Path, tmp_path: Path
    ):
        """上限を超えた構成では学習も記録も始めないこと."""

        run_directory = tmp_path / "run"
        logger = RecordingExperimentLogger()

        run_training(
            _composed(dataset_root, run_directory, "model.stage_channels=[48, 512]"),
            logger=logger,
            device=DEVICE,
        )

        assert logger.started is False
        assert not (run_directory / "latest.pt").exists()
        assert not (run_directory / CONFIG_FILE_NAME).exists()


class TestInitialWeights:
    """Fine-tune の起点になる weight."""

    def test_it_starts_from_the_weights_of_another_run(
        self, dataset_root: Path, tmp_path: Path
    ):
        """``model.initial_weights`` を渡した run が、その重みから始まること.

        観測点は「1 epoch だけ回した run の best が、起点の重みと違い、かつ起点を渡さない run とも違う」こと。
        """

        source = tmp_path / "source"
        _trained(dataset_root, source)
        target = tmp_path / "target"

        _trained(
            dataset_root,
            target,
            f"model.initial_weights={source / WEIGHTS_FILE_NAME}",
            "trainer.max_epochs=1",
            "trainer.learning_rate=0.0",
        )

        # 学習率 0 なので重みは起点のまま動かない。
        weights, _ = load_model_weights(source / WEIGHTS_FILE_NAME)
        assert weights is not None
        _assert_same_weights(
            _state_dict(CheckpointStore(target), "best"), weights.model_state
        )

    def test_it_rejects_a_state_dict_with_a_different_key_set(
        self, dataset_root: Path, tmp_path: Path
    ):
        source = tmp_path / "source"
        _trained(dataset_root, source)
        weights, _ = load_model_weights(source / WEIGHTS_FILE_NAME)
        assert weights is not None
        truncated = dict(weights.model_state)
        removed = sorted(truncated)[0]
        del truncated[removed]
        broken = tmp_path / "broken.pt"
        save_model_weights(
            broken,
            ModelWeights(
                model_family=weights.model_family,
                model_config=weights.model_config,
                constraints=weights.constraints,
                dataset_fingerprint=weights.dataset_fingerprint,
                model_state=truncated,
            ),
        )

        outcome, error = run_training(
            _composed(
                dataset_root, tmp_path / "target", f"model.initial_weights={broken}"
            ),
            logger=RecordingExperimentLogger(),
            device=DEVICE,
        )

        assert outcome is None
        assert error is not None
        assert removed in error


class TestInterruptionParity:
    """中断して再開しても、通し実行と同じ重みと metric に到達する."""

    def test_the_hook_interrupts_the_run_in_the_middle_of_an_epoch(
        self, dataset_root: Path, tmp_path: Path
    ):
        """中断が実際に起きたことを、weight の一致とは別に観測する.

        一致の検査は中断が no-op でも緑になる。停止理由・記録先の状態・ checkpoint の進捗・学習 forward
        の回数の 4 つで、止まった位置まで固定する。
        """

        run_directory = tmp_path / "interrupted"
        with _terminating_at(2) as counted:
            outcome, logger = _trained(dataset_root, run_directory)
        checkpoint, reason = CheckpointStore(run_directory).load("latest")

        assert outcome.stop_reason == "signal"
        assert logger.status == "KILLED"
        assert counted[0] == 2
        assert reason is None
        assert checkpoint is not None
        assert checkpoint.progress.epochs_completed == 0
        assert (
            0
            < checkpoint.progress.next_batch_index
            < len(checkpoint.progress.batch_plan)
        )

    def test_the_run_finishes_when_the_hook_never_fires(
        self, dataset_root: Path, tmp_path: Path
    ):
        """観測器の自己検査。中断を仕込まなければ通しで終わること.

        上の検査が「常に signal で止まる」ようになっていないことを示す。
        """

        with _terminating_at(10_000) as counted:
            outcome, logger = _trained(dataset_root, tmp_path / "reference")

        assert outcome.stop_reason == "max_epochs"
        assert logger.status == "FINISHED"
        assert counted[0] > 0

    def test_resume_reaches_the_uninterrupted_weights_and_metrics(
        self, dataset_root: Path, tmp_path: Path
    ):
        """``resume.checkpoint`` から続けた run が通し実行に一致すること."""

        reference = tmp_path / "reference"
        expected, _ = _trained(dataset_root, reference)
        interrupted = tmp_path / "interrupted"
        with _terminating_at(2):
            first, _ = _trained(dataset_root, interrupted)

        second, _ = _trained(
            dataset_root,
            interrupted,
            f"resume.checkpoint={interrupted / 'latest.pt'}",
        )

        assert first.stop_reason == "signal"
        assert second.stop_reason == "max_epochs"
        assert second.global_step == expected.global_step
        assert dict(second.last_validation_metrics) == dict(
            expected.last_validation_metrics
        )
        _assert_same_weights(
            _state_dict(CheckpointStore(interrupted), "final"),
            _state_dict(CheckpointStore(reference), "final"),
        )

    def test_the_interrupted_run_had_not_reached_those_weights(
        self, dataset_root: Path, tmp_path: Path
    ):
        """中断した時点では通し実行と違う重みだったこと.

        これが成り立たないと、上の一致は「中断しても何も起きなかった」ことの言い換えになる。
        """

        reference = tmp_path / "reference"
        _trained(dataset_root, reference)
        interrupted = tmp_path / "interrupted"
        with _terminating_at(2):
            _trained(dataset_root, interrupted)

        with pytest.raises(AssertionError):
            _assert_same_weights(
                _state_dict(CheckpointStore(interrupted), "final"),
                _state_dict(CheckpointStore(reference), "final"),
            )


class TestSeeding:
    """同じ argv なら、周囲の乱数状態に関わらず同じ run になること."""

    def test_it_ignores_the_ambient_random_state(
        self, dataset_root: Path, tmp_path: Path
    ):
        """Model の初期化まで run seed で決まること.

        初期化は ``Trainer.run`` が seed を撒くより前に起きるので、
        entrypoint 側で撒かないと初期重みが process の周囲の状態で変わる。

        fold ごとに違う重みから始まった run は、metric を並べても比較できない。
        """

        torch.manual_seed(1234)
        _trained(dataset_root, tmp_path / "first")
        torch.manual_seed(4321)
        _trained(dataset_root, tmp_path / "second")

        _assert_same_weights(
            _state_dict(CheckpointStore(tmp_path / "first"), "final"),
            _state_dict(CheckpointStore(tmp_path / "second"), "final"),
        )

    def test_a_different_seed_reaches_different_weights(
        self, dataset_root: Path, tmp_path: Path
    ):
        """Seed を変えれば結果が変わること.

        上の一致が「どんな設定でも同じ重みへ行き着く」形へ退化していないか。
        """

        _trained(dataset_root, tmp_path / "first")
        _trained(dataset_root, tmp_path / "second", "trainer.seed=7")

        with pytest.raises(AssertionError):
            _assert_same_weights(
                _state_dict(CheckpointStore(tmp_path / "first"), "final"),
                _state_dict(CheckpointStore(tmp_path / "second"), "final"),
            )


class TestCalibration:
    """学習後に validation split だけで fit する log 分散 offset."""

    def test_it_fits_the_offset_on_the_validation_split(
        self, dataset_root: Path, tmp_path: Path
    ):
        run_directory = tmp_path / "run"

        _trained(dataset_root, run_directory)

        calibration, error = UncertaintyCalibration.load(
            run_directory / CALIBRATION_FILE_NAME
        )
        assert error is None, error
        assert calibration is not None
        assert calibration.split == "validation"
        assert calibration.sample_count > 0
        assert calibration.coverage_before != calibration.coverage_after


class TestMainArguments:
    """Argv の不備を理由文字列で返すこと."""

    def test_it_refuses_to_train_without_a_logger(
        self, dataset_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """記録先を選ばない run を、黙って動かさないこと.

        「logging が黙って無効」は最も避けたい失敗の形なので、no-op logger を置かずに理由で落とす。
        """

        code = main(
            [
                "experiment=base",
                f'data.roots=["{dataset_root}"]',
                f"data.held_out_session={HELD_OUT}",
                f"run_directory={tmp_path / 'run'}",
            ]
        )

        assert code == 1
        assert "logger" in capsys.readouterr().err

    def test_it_refuses_an_unknown_key(
        self, dataset_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = main(
            [
                "experiment=base",
                f'data.roots=["{dataset_root}"]',
                "data.held_out_sessions=typo",
                f"run_directory={tmp_path / 'run'}",
            ]
        )

        assert code == 1
        assert "held_out_sessions" in capsys.readouterr().err

    def test_it_refuses_a_resume_checkpoint_that_is_not_there(
        self, dataset_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = main(
            [
                "experiment=base",
                "logger=mlflow",
                f"logger.tracking_uri=sqlite:///{tmp_path}/mlflow.db",
                f"logger.artifact_location={tmp_path}/mlartifacts",
                f'data.roots=["{dataset_root}"]',
                f"data.held_out_session={HELD_OUT}",
                f"run_directory={tmp_path / 'run'}",
                f"resume.checkpoint={tmp_path / 'missing.pt'}",
            ]
        )

        assert code == 1
        assert "missing.pt" in capsys.readouterr().err
