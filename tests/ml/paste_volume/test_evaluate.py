"""評価 entrypoint の公開契約.

Fold は実際に ``run_training`` で作る。

run directory の中身（config.json / split.json / best.pt / calibration.json）を
手で組むと、学習側が書式を変えたときに評価側だけが緑のまま取り残される。

``split=test`` の拒否は「到達しない」型の検査なので、同じ argv から flag だけを
足した経路が通ることを対で置く（``memory/negative-assertion-needs-self-check.md``）。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path

import attrs
import pytest
import torch

from ml.config.composition import ConfigComposition
from ml.paste_volume.evaluate import (
    CrossValidationReport,
    EvaluationRequest,
    evaluate_fold,
    evaluate_request,
    main,
)
from ml.paste_volume.experiment import (
    CALIBRATION_FILE_NAME,
    CONFIG_FILE_NAME,
    compose_experiment,
    load_experiment_config,
    save_experiment_config,
)
from ml.paste_volume.model import MODEL_FAMILY
from ml.paste_volume.train import run_training
from ml.serialization import make_strict_converter
from tests.ml.helpers import skip_if_no_inductor
from tests.ml.paste_volume.helpers import write_synthetic_sessions
from tests.ml.support import RecordingExperimentLogger

DEVICE = torch.device("cpu")

SESSION_LABELS = ("session-0", "session-1", "session-2")


@pytest.fixture(scope="module")
def folds(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """3 fold ぶんの run directory を実際に学習して作る."""

    base = tmp_path_factory.mktemp("paste-volume-evaluate")
    dataset_root = base / "data"
    write_synthetic_sessions(dataset_root, session_count=len(SESSION_LABELS))
    runs = base / "runs"
    for label in SESSION_LABELS:
        config, error = compose_experiment(
            [
                "experiment=base",
                f'data.roots=["{dataset_root}"]',
                f"data.held_out_session={label}",
                "data.max_batch_size=4",
                "trainer.max_epochs=2",
                "trainer.compile_enabled=false",
                f"run_directory={runs / label}",
            ]
        )
        assert error is None, error
        assert config is not None
        _, error = run_training(
            config, logger=RecordingExperimentLogger(), device=DEVICE
        )
        assert error is None, error
    return runs


def _requested(*arguments: str) -> EvaluationRequest:
    request, error = ConfigComposition(overrides=arguments).structure(
        EvaluationRequest, converter=make_strict_converter()
    )

    assert error is None, error
    assert request is not None
    return request


def _report(*arguments: str) -> CrossValidationReport:
    report, error = evaluate_request(_requested(*arguments), device=DEVICE)

    assert error is None, error
    assert report is not None
    return report


class TestCrossValidationReport:
    """``folds=`` から作る交差検証 report."""

    def test_it_reports_one_fold_per_run_directory(self, folds: Path):
        report = _report(f"folds={folds}", "split=test", "allow_frozen_test=true")

        assert [fold.run_name for fold in report.folds] == list(SESSION_LABELS)
        assert [fold.held_out_session for fold in report.folds] == list(SESSION_LABELS)
        assert report.model_family == MODEL_FAMILY
        assert report.split == "test"

    def test_each_test_split_holds_only_the_held_out_session(self, folds: Path):
        """Fold の test split が held-out session だけで出来ていること.

        session slice は予測の並びから引き直すので、slice の値が 1 種類しか無いことが「test に他
        session が混ざっていない」ことの観測になる。
        """

        report = _report(f"folds={folds}", "split=test", "allow_frozen_test=true")

        for fold in report.folds:
            values = {item.value for item in fold.session_slices}
            assert values == {fold.held_out_session}
            assert sum(item.sample_count for item in fold.session_slices) == (
                fold.sample_count
            )

    def test_the_validation_split_is_a_different_session(self, folds: Path):
        """Validation split が held-out 以外の session であること."""

        report = _report(f"folds={folds}", "split=validation")

        for fold in report.folds:
            values = {item.value for item in fold.session_slices}
            assert fold.held_out_session not in values
            assert len(values) == 1

    def test_the_aggregate_carries_the_mean_and_the_spread(self, folds: Path):
        report = _report(f"folds={folds}", "split=test", "allow_frozen_test=true")

        aggregate = report.aggregate()
        assert aggregate["fold_count"] == len(SESSION_LABELS)
        assert aggregate["mean_absolute_error_mean"] > 0
        assert aggregate["mean_absolute_error_standard_deviation"] >= 0
        assert "negative_log_likelihood_mean" in aggregate

    def test_it_round_trips_through_the_document(self, folds: Path, tmp_path: Path):
        report = _report(f"folds={folds}", "split=validation")
        path = tmp_path / "report.json"
        converter = make_strict_converter()

        report.save(path, converter=converter)

        loaded, error = CrossValidationReport.load(path, converter=converter)
        assert error is None, error
        assert loaded == report

    def test_it_ignores_directories_without_a_checkpoint(
        self, folds: Path, tmp_path: Path
    ):
        """Run directory の隣に置いた成果物を fold として数えないこと."""

        staging = tmp_path / "runs"
        shutil.copytree(folds, staging)
        (staging / "mlruns").mkdir()

        report = _report(f"folds={staging}", "split=validation")

        assert len(report.folds) == len(SESSION_LABELS)


class TestFrozenTestSplit:
    """``split=test`` は明示の許可を要求する."""

    def test_it_refuses_the_test_split_without_the_flag(self, folds: Path):
        report, error = evaluate_request(
            _requested(f"folds={folds}", "split=test"), device=DEVICE
        )

        assert report is None
        assert error is not None
        assert "allow_frozen_test" in error

    def test_the_same_arguments_pass_with_the_flag(self, folds: Path):
        """許可を足すだけで通ること.

        拒否の検査が「test split では常に失敗する」形に退化していないことを同じ argv で確かめる。
        """

        report, error = evaluate_request(
            _requested(f"folds={folds}", "split=test", "allow_frozen_test=true"),
            device=DEVICE,
        )

        assert error is None, error
        assert report is not None

    def test_the_other_splits_do_not_need_the_flag(self, folds: Path):
        """許可の要求が test split だけに掛かっていること."""

        report, error = evaluate_request(
            _requested(f"folds={folds}", "split=validation"), device=DEVICE
        )

        assert error is None, error
        assert report is not None

    def test_it_requires_exactly_one_selector(self, folds: Path):
        request = _requested(
            f"folds={folds}", f"checkpoint={folds / 'session-0' / 'best.pt'}"
        )

        assert request.validate() is not None


class TestSingleCheckpoint:
    """``checkpoint=`` で 1 run だけを測る."""

    def test_it_evaluates_the_run_that_owns_the_checkpoint(self, folds: Path):
        report = _report(
            f"checkpoint={folds / 'session-1' / 'best.pt'}", "split=validation"
        )

        assert len(report.folds) == 1
        assert report.folds[0].run_name == "session-1"

    def test_it_reports_a_checkpoint_that_is_not_there(self, folds: Path):
        report, error = evaluate_request(
            _requested(f"checkpoint={folds / 'session-9' / 'best.pt'}"), device=DEVICE
        )

        assert report is None
        assert error is not None
        assert "session-9" in error


class TestCalibrationIsApplied:
    """記録した log 分散 offset を足してから測ること."""

    def test_it_uses_the_offset_the_run_fitted(self, folds: Path):
        fold, error = evaluate_fold(folds / "session-0", split="test", device=DEVICE)

        assert error is None, error
        assert fold is not None
        assert fold.log_variance_offset is not None

    def test_it_reports_a_calibration_it_cannot_read(self, folds: Path, tmp_path: Path):
        """壊れた calibration.json を黙って無視しないこと.

        ``log_variance_offset`` は report に載る値なので、file が壊れた fold と
        calibration を作らなかった fold が report 上で同じ形になってはならない。
        """

        staging = tmp_path / "run"
        shutil.copytree(folds / "session-0", staging)
        (staging / CALIBRATION_FILE_NAME).write_text("{}", encoding="utf-8")

        fold, error = evaluate_fold(staging, split="validation", device=DEVICE)

        assert fold is None
        assert error is not None
        assert CALIBRATION_FILE_NAME in error or "document" in error

    def test_dropping_the_calibration_changes_the_coverage(
        self, folds: Path, tmp_path: Path
    ):
        """Offset が実際に metric を動かしていること.

        offset を読んでも使わない実装でも、上の検査は緑のままになる。
        """

        staging = tmp_path / "run"
        shutil.copytree(folds / "session-0", staging)
        with_offset, _ = evaluate_fold(staging, split="test", device=DEVICE)
        (staging / CALIBRATION_FILE_NAME).unlink()

        without_offset, error = evaluate_fold(staging, split="test", device=DEVICE)

        assert error is None, error
        assert with_offset is not None and without_offset is not None
        assert without_offset.log_variance_offset is None
        assert (
            with_offset.metrics.mean_predicted_standard_deviation
            != without_offset.metrics.mean_predicted_standard_deviation
        )
        assert (
            with_offset.metrics.mean_absolute_error
            == without_offset.metrics.mean_absolute_error
        )


class TestNamedCheckpoint:
    """``checkpoint=`` は名指しした file そのものを測る."""

    def test_it_measures_the_named_file_instead_of_the_best(
        self, folds: Path, tmp_path: Path
    ):
        """別 fold の重みを持ち込むと結果が変わること.

        ``final.pt`` と ``best.pt`` は同じ重みなので（``Trainer`` は best を読み
        戻してから final を書く）、その 2 つの比較では「role best を決め打ちで
        読む」実装と区別できない。読む file を変えたら結果が変わることを、
        中身の違う checkpoint で見る。
        """

        staging = tmp_path / "run"
        shutil.copytree(folds / "session-0", staging)
        shutil.copy(folds / "session-1" / "best.pt", staging / "other.pt")

        own, error = evaluate_fold(staging, split="validation", device=DEVICE)
        assert error is None, error
        other, error = evaluate_fold(
            staging,
            split="validation",
            checkpoint=staging / "other.pt",
            device=DEVICE,
        )

        assert error is None, error
        assert own is not None and other is not None
        assert own.metrics.mean_absolute_error != other.metrics.mean_absolute_error

    def test_the_default_is_the_best_checkpoint(self, folds: Path, tmp_path: Path):
        """名指ししなければ best.pt を測ること.

        上の「file を変えたら変わる」が、``checkpoint=`` を渡すと必ず別の値に
        なる形へ退化していないか。
        """

        staging = tmp_path / "run"
        shutil.copytree(folds / "session-0", staging)

        default, error = evaluate_fold(staging, split="validation", device=DEVICE)
        assert error is None, error
        named, error = evaluate_fold(
            staging,
            split="validation",
            checkpoint=staging / "best.pt",
            device=DEVICE,
        )

        assert error is None, error
        assert default is not None and named is not None
        assert default.metrics == named.metrics

    def test_it_reports_a_checkpoint_of_another_model(
        self, folds: Path, tmp_path: Path
    ):
        """Config と食い違う checkpoint を理由文字列で返すこと.

        ``load_state_dict`` の既定は例外だが、evaluate も entrypoint なので
        学習側の ``model.initial_weights`` と同じ契約にする。
        """

        staging = tmp_path / "run"
        shutil.copytree(folds / "session-0", staging)
        config, error = load_experiment_config(staging / CONFIG_FILE_NAME)
        assert error is None, error
        assert config is not None
        save_experiment_config(
            attrs.evolve(
                config,
                model=attrs.evolve(config.model, blocks_per_stage=(2, 3)),
            ),
            staging / CONFIG_FILE_NAME,
        )

        fold, error = evaluate_fold(staging, split="validation", device=DEVICE)

        assert fold is None
        assert error is not None
        assert "キー集合" in error


class TestTrainingAndEvaluationAgree:
    """学習 loop と評価 entrypoint が同じ数字を出すこと.

    report は step 8 の判定に直結するので、2 つの評価経路を結び付けておく。

    calibration の offset は評価側でだけ足すので、平均側の metric は一致し、不確かさ側（NLL /
    coverage）は動く。
    """

    @pytest.fixture(scope="class")
    def single_epoch_fold(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> tuple[Path, Mapping[str, float]]:
        """1 epoch だけ回した fold と、その run が出した validation metric.

        best epoch と最終 epoch を同じにしておかないと、``best.pt`` を測る評価と
        「最後の epoch の validation metric」は原理的に一致しない。
        """

        base = tmp_path_factory.mktemp("paste-volume-agreement")
        dataset_root = base / "data"
        write_synthetic_sessions(dataset_root, session_count=len(SESSION_LABELS))
        run_directory = base / "run"
        config, error = compose_experiment(
            [
                "experiment=base",
                f'data.roots=["{dataset_root}"]',
                f"data.held_out_session={SESSION_LABELS[0]}",
                "data.max_batch_size=4",
                "trainer.max_epochs=1",
                "trainer.compile_enabled=false",
                f"run_directory={run_directory}",
            ]
        )
        assert error is None, error
        assert config is not None
        outcome, error = run_training(
            config, logger=RecordingExperimentLogger(), device=DEVICE
        )
        assert error is None, error
        assert outcome is not None
        return run_directory, dict(outcome.last_validation_metrics)

    def test_the_report_repeats_the_validation_metrics_of_the_loop(
        self, single_epoch_fold: tuple[Path, Mapping[str, float]]
    ):
        run_directory, expected = single_epoch_fold

        fold, error = evaluate_fold(run_directory, split="validation", device=DEVICE)

        assert error is None, error
        assert fold is not None
        assert fold.metrics.mean_absolute_error == pytest.approx(
            expected["mean_absolute_error"]
        )
        assert fold.metrics.root_mean_squared_error == pytest.approx(
            expected["root_mean_squared_error"]
        )
        assert fold.metrics.sample_count == expected["sample_count"]
        assert fold.metrics.valid_sample_count == expected["valid_sample_count"]

    def test_the_uncertainty_metrics_differ_by_the_calibration(
        self, single_epoch_fold: tuple[Path, Mapping[str, float]]
    ):
        """一致するのは calibration が触らない側だけであること.

        上の一致が「評価が学習 loop の値をそのまま写している」形ではないことを示す。offset は validation で
        fit した値なので、同じ split でも NLL と coverage は動く。
        """

        run_directory, expected = single_epoch_fold

        fold, error = evaluate_fold(run_directory, split="validation", device=DEVICE)

        assert error is None, error
        assert fold is not None
        assert fold.log_variance_offset is not None
        assert fold.metrics.negative_log_likelihood != pytest.approx(
            expected["negative_log_likelihood"]
        )


class TestCompiledRun:
    """``compile_enabled=true`` で学習した run を評価まで通せること.

    step 8 が実際に使う設定（``trainer=gpu`` は compile 既定 ON）の end-to-end。

    compile 済み model の ``state_dict`` は ``_orig_mod.`` 接頭辞を持つので、
    checkpoint の書き出しと config.json からの組み直しのどちらかが接頭辞を
    取りこぼすと、評価側で初めて落ちる。

    可変 shape の batch は recompile 上限に当たって以降 eager で走るが、
    compile 経路を 1 度は通る。
    """

    @skip_if_no_inductor
    def test_a_compiled_run_evaluates_from_its_configuration(self, tmp_path: Path):
        dataset_root = tmp_path / "data"
        write_synthetic_sessions(dataset_root, session_count=len(SESSION_LABELS))
        run_directory = tmp_path / "run"
        config, error = compose_experiment(
            [
                "experiment=base",
                "trainer=gpu",
                f'data.roots=["{dataset_root}"]',
                f"data.held_out_session={SESSION_LABELS[0]}",
                "data.max_batch_size=4",
                "trainer.max_epochs=1",
                "trainer.automatic_mixed_precision_enabled=false",
                f"run_directory={run_directory}",
            ]
        )
        assert error is None, error
        assert config is not None
        assert config.trainer.compile_enabled is True
        outcome, error = run_training(
            config, logger=RecordingExperimentLogger(), device=DEVICE
        )
        assert error is None, error
        assert outcome is not None

        fold, error = evaluate_fold(run_directory, split="test", device=DEVICE)

        assert error is None, error
        assert fold is not None
        assert fold.sample_count > 0
        assert fold.log_variance_offset is not None


class TestDataRootOverride:
    """収集 session の所在だけを差し替える."""

    def test_it_reads_the_sessions_from_the_given_roots(
        self, folds: Path, tmp_path: Path
    ):
        """Dataset を別の場所へ移しても、同じ split で測り直せること.

        dataset fingerprint は内容だけから決まるので、複製した dataset は split manifest
        の照合を通る。
        """

        moved = tmp_path / "moved"
        shutil.copytree(folds.parent / "data", moved)

        report = _report(
            f"folds={folds}", "split=validation", f'data.roots=["{moved}"]'
        )

        assert len(report.folds) == len(SESSION_LABELS)

    def test_it_reports_roots_without_a_session(self, folds: Path, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()

        report, error = evaluate_request(
            _requested(f"folds={folds}", f'data.roots=["{empty}"]'), device=DEVICE
        )

        assert report is None
        assert error is not None
        assert "session" in error


class TestMainArguments:
    """Argv からの実行."""

    def test_it_writes_the_report_and_prints_the_aggregate(
        self, folds: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        output = tmp_path / "nested" / "report.json"

        code = main(
            [
                f"folds={folds}",
                "split=test",
                "allow_frozen_test=true",
                f"output={output}",
            ]
        )

        assert code == 0
        assert output.is_file()
        printed = json.loads(capsys.readouterr().out)
        assert printed["fold_count"] == len(SESSION_LABELS)

    def test_it_refuses_an_unknown_key(
        self, folds: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = main([f"folds={folds}", "split_name=test"])

        assert code == 1
        assert "split_name" in capsys.readouterr().err
