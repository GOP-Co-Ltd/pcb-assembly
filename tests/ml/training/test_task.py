"""学習 task ABC と Gaussian 回帰 task の公開契約."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import override

import attrs
import pytest
import torch
from torch import Tensor, nn

from ml.evaluation.compile_parity import CompileOptions
from ml.evaluation.regression import GaussianRegressionMetrics, MeanSaturationDiagnostic
from ml.training.task import (
    GaussianBatch,
    GaussianObservation,
    GaussianRegressionTask,
    StepResult,
    TrainingTask,
)
from tests.ml.support import (
    SyntheticDatasetOptions,
    SyntheticRegressionData,
    build_synthetic_model,
)

EAGER_OPTIONS = CompileOptions(backend="eager")
DEVICE = torch.device("cpu")

METRIC_FIELD_NAMES = frozenset(
    field.name for field in attrs.fields(GaussianRegressionMetrics)
)

# 平均 head が死んだ run の原因を運用者へ見せる診断。metric が 1 つも出せないときでも
# 残る必要があるので、回帰 metric とは別枠で数える
SATURATION_FIELD_NAMES = frozenset(
    field.name for field in attrs.fields(MeanSaturationDiagnostic)
)


class _IncompleteTask(TrainingTask[GaussianBatch, GaussianObservation]):
    """``model`` しか実装していない、契約違反の検証用 task."""

    @property
    @override
    def model(self) -> nn.Module:
        return nn.Linear(1, 1)


class _MinimalTask(TrainingTask[Tensor, Tensor]):
    """``compile_forward`` を override しない最小の task.

    ABC の具象既定が no-op であることを確かめるために使う。
    """

    def __init__(self) -> None:
        self._model = nn.Linear(2, 1)

    @property
    @override
    def model(self) -> nn.Module:
        return self._model

    @override
    def training_step(self, batch: Tensor) -> StepResult[Tensor]:
        output: Tensor = self._model(batch)
        return StepResult(
            loss=output.sum(),
            observation=output.detach(),
            sample_count=int(batch.shape[0]),
        )

    @override
    def evaluation_step(self, batch: Tensor) -> Tensor:
        with torch.no_grad():
            output: Tensor = self._model(batch)
        return output

    @override
    def reduce(self, observations: Sequence[Tensor]) -> Mapping[str, float]:
        if not observations:
            return {}
        return {"mean": float(torch.cat(list(observations)).mean().item())}


def _batch(sample_count: int = 3) -> GaussianBatch:
    data = SyntheticRegressionData(SyntheticDatasetOptions())
    sample_ids = data.sample_ids_for("train")[:sample_count]
    return data.materialize(
        sample_ids, split="train", epoch=0, training=False, device=DEVICE
    )


def _task() -> GaussianRegressionTask:
    return GaussianRegressionTask(build_synthetic_model(seed=3))


class TestStepResult:
    """1 step の戻り値は使う前に理由つきで検証できる."""

    def test_zero_dimensional_loss_with_positive_samples_is_valid(self):
        result = StepResult(
            loss=torch.tensor(1.5),
            observation=object(),
            sample_count=3,
        )

        assert result.validate() is None

    def test_non_scalar_loss_is_rejected(self):
        result = StepResult(
            loss=torch.zeros(2),
            observation=object(),
            sample_count=3,
        )

        reason = result.validate()

        assert reason is not None
        assert "loss" in reason

    def test_non_positive_sample_count_is_rejected(self):
        result = StepResult(
            loss=torch.tensor(1.0),
            observation=object(),
            sample_count=0,
        )

        reason = result.validate()

        assert reason is not None
        assert "sample_count" in reason


class TestGaussianBatch:
    """Batch の shape と dtype は使う前に理由つきで検証できる."""

    def test_consistent_batch_is_valid(self):
        assert _batch().validate() is None

    def test_target_shape_mismatch_is_rejected(self):
        batch = _batch()
        broken = attrs.evolve(batch, target=torch.ones(batch.target.shape[0] + 1, 1))

        reason = broken.validate()

        assert reason is not None
        assert "target" in reason

    def test_non_boolean_mask_is_rejected(self):
        batch = _batch()
        assert batch.valid_pixel_mask is not None
        broken = attrs.evolve(
            batch, valid_pixel_mask=batch.valid_pixel_mask.to(torch.float32)
        )

        reason = broken.validate()

        assert reason is not None
        assert "bool" in reason

    def test_sample_weight_shape_mismatch_is_rejected(self):
        batch = _batch()
        broken = attrs.evolve(batch, sample_weight=torch.ones(1, 1))

        reason = broken.validate()

        assert reason is not None
        assert "sample_weight" in reason


class TestTrainingTaskContract:
    """``TrainingTask`` は実装漏れを instantiate 時に弾く."""

    def test_partial_implementation_cannot_be_instantiated(self):
        with pytest.raises(TypeError) as exception:
            _IncompleteTask()  # pyright: ignore[reportAbstractUsage]

        assert "abstract" in str(exception.value).lower()

    def test_gaussian_task_implements_the_abstract_methods(self):
        assert isinstance(_task(), TrainingTask)


class TestGaussianRegressionTaskTraining:
    """学習 step は微分可能な 0 次元 loss と切り離した観測を返す."""

    def test_training_step_returns_a_differentiable_scalar_loss(self):
        task = _task()
        batch = _batch()

        result = task.training_step(batch)

        assert result.validate() is None
        assert result.loss.ndim == 0
        assert result.loss.requires_grad is True
        assert result.sample_count == int(batch.target.shape[0])

    def test_training_observation_is_detached(self):
        task = _task()

        result = task.training_step(_batch())

        assert result.observation.mean.requires_grad is False
        assert result.observation.log_variance.requires_grad is False

    def test_backward_reaches_the_model_parameters(self):
        task = _task()

        task.training_step(_batch()).loss.backward()

        assert any(parameter.grad is not None for parameter in task.model.parameters())


class TestGaussianRegressionTaskEvaluation:
    """評価 step は勾配を作らず、集計は metric のキーをそのまま返す."""

    def test_evaluation_step_creates_no_gradient(self):
        task = _task()

        observation = task.evaluation_step(_batch())

        assert observation.mean.requires_grad is False
        assert observation.mean.grad_fn is None

    def test_reduce_keys_match_the_regression_metric_fields(self):
        task = _task()
        observations = [task.evaluation_step(_batch()) for _ in range(2)]

        metrics = task.reduce(observations)

        assert set(metrics) == METRIC_FIELD_NAMES | SATURATION_FIELD_NAMES
        assert all(isinstance(value, float) for value in metrics.values())

    def test_a_healthy_run_reports_no_saturated_positive_target(self):
        """平均が生きている run では飽和割合が 0.0 として残る.

        失敗したときにだけ現れる指標は、run をまたいで推移を追えない。

        正常時も同じ key で出しておくことで、値が 0.0 から動いた瞬間に気付ける。
        """

        task = _task()

        metrics = task.reduce([task.evaluation_step(_batch())])

        assert metrics["saturated_positive_fraction"] == 0.0

    def test_a_fully_saturated_evaluation_still_reports_why(self):
        """全 sample の平均が 0 でも、原因が読み取れる情報を残す.

        ``valid_sample_mask`` は ``mean > 0`` を要求するので、平均 head が死ぬと
        回帰 metric が 1 つも出せない。

        そのまま空の写像を返すと、運用者が受け取るのは「monitor がありません」
        だけになり、真の原因である飽和が見えない。

        主要 monitor を欠かせたまま（Trainer は従来どおり大きな音で失敗する）、
        原因だけを残すのがこの契約。
        """

        task = _task()
        observation = task.evaluation_step(_batch())
        saturated = attrs.evolve(observation, mean=torch.zeros_like(observation.mean))

        metrics = task.reduce([saturated])

        assert metrics != {}
        assert "relative_error_score" not in metrics
        assert set(metrics) == SATURATION_FIELD_NAMES
        assert metrics["saturated_positive_fraction"] == 1.0
        assert metrics["positive_target_count"] == float(observation.mean.numel())

    def test_reduce_without_observations_returns_an_empty_mapping(self):
        task = _task()

        metrics = task.reduce([])

        assert metrics == {}

    def test_reduce_reports_nothing_for_misaligned_observations(self):
        """長さのそろわない観測は集計せず空の写像を返す.

        飽和診断は長さ検証を呼び出し側の責務にしている。

        この guard を飛ばすと tensor の broadcast 例外になる。
        """

        task = _task()
        observation = task.evaluation_step(_batch())
        misaligned = attrs.evolve(
            observation, target=torch.cat([observation.target, torch.ones(1, 1)])
        )

        assert task.reduce([misaligned]) == {}


class TestCompileSeam:
    """Compile しても state を触る対象は元の module のまま保たれる."""

    def test_default_compile_forward_is_a_no_op(self):
        task = _MinimalTask()
        before = task.model
        inputs = torch.ones(2, 2)
        expected = task.training_step(inputs).loss

        task.compile_forward(EAGER_OPTIONS)

        assert task.model is before
        assert torch.equal(task.training_step(inputs).loss, expected)

    def test_model_property_stays_the_uncompiled_module(self):
        task = _task()
        before_keys = sorted(task.model.state_dict())

        task.compile_forward(EAGER_OPTIONS)

        assert sorted(task.model.state_dict()) == before_keys
        assert all(not key.startswith("_orig_mod.") for key in before_keys)

    def test_compiled_task_still_produces_a_differentiable_loss(self):
        task = _task()

        task.compile_forward(EAGER_OPTIONS)
        result = task.training_step(_batch())

        assert result.loss.requires_grad is True


class TestObservationTyping:
    """PEP 695 ジェネリクスの ``StepResult`` が実行時に組み立てられる."""

    def test_step_result_carries_the_observation_type(self):
        task = _task()

        result: StepResult[GaussianObservation] = task.training_step(_batch())
        observations: Sequence[GaussianObservation] = [result.observation]
        metrics: Mapping[str, float] = task.reduce(observations)

        assert isinstance(result.observation, GaussianObservation)
        assert isinstance(result.observation.target, Tensor)
        assert set(metrics) == METRIC_FIELD_NAMES | SATURATION_FIELD_NAMES
