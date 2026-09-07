"""Eager と ``torch.compile`` の一致比較の公開契約."""

import time
from typing import override

import attrs
import pytest
import torch
from torch import Tensor, nn

from ml.evaluation.compile_parity import (
    FLOAT32_PARITY_TOLERANCES,
    CompileOptions,
    CompileParityResult,
    CompileParityTolerances,
    ParityTolerance,
    TensorDifference,
)
from ml.model.blocks import ImageEncoder, ImageEncoderConfig
from ml.model.heads import (
    GaussianHeadConfig,
    GaussianImageRegressor,
    GaussianRegressionHead,
)
from ml.model.loss import weighted_gaussian_negative_log_likelihood
from tests.ml.helpers import skip_if_no_inductor

# Raspberry Pi 5 の CPU で数秒に収まる規模に保つ
ENCODER_CONFIG = ImageEncoderConfig(
    input_channels=3,
    stem_channels=(8,),
    stem_strides=(2,),
    stage_channels=(8, 16),
    stage_strides=(1, 2),
    blocks_per_stage=(1, 1),
    group_norm_groups=4,
)
HEAD_CONFIG = GaussianHeadConfig(
    input_features=ENCODER_CONFIG.output_features,
    conditioning_features=1,
    hidden_features=16,
)
EAGER_OPTIONS = CompileOptions(backend="eager")
SAMPLE_COUNT = 2
IMAGE_SIZE = 16


def _regressor() -> GaussianImageRegressor:
    torch.manual_seed(11)
    encoder = ImageEncoder(ENCODER_CONFIG)
    head = GaussianRegressionHead(HEAD_CONFIG)
    return GaussianImageRegressor(encoder, head).eval()


def _inputs() -> tuple[Tensor, Tensor, Tensor]:
    generator = torch.Generator().manual_seed(13)
    images = torch.randn(
        (SAMPLE_COUNT, ENCODER_CONFIG.input_channels, IMAGE_SIZE, IMAGE_SIZE),
        generator=generator,
    )
    # 一部を padding 扱いにして、学習可能 padding pixel にも勾配を流す
    valid_pixel_mask = torch.ones(
        (SAMPLE_COUNT, 1, IMAGE_SIZE, IMAGE_SIZE), dtype=torch.bool
    )
    valid_pixel_mask[1, :, IMAGE_SIZE // 2 :, :] = False
    conditioning = torch.randn((SAMPLE_COUNT, 1), generator=generator)
    return images, valid_pixel_mask, conditioning


def _gaussian_loss(outputs: tuple[Tensor, ...]) -> Tensor:
    mean, log_variance = outputs
    target = torch.full_like(mean, 3.0)
    sample_weight = torch.ones_like(mean)
    return weighted_gaussian_negative_log_likelihood(
        mean, log_variance, target, sample_weight
    )


def _compare(
    model: nn.Module,
    inputs: tuple[Tensor, ...],
    options: CompileOptions = EAGER_OPTIONS,
) -> CompileParityResult:
    result, reason = CompileParityResult.measure(
        model,
        inputs,
        loss=_gaussian_loss,
        tolerances=FLOAT32_PARITY_TOLERANCES,
        options=options,
    )

    assert reason is None
    assert result is not None
    return result


class _UnusedParameterModel(nn.Module):
    """両側とも勾配が ``None`` になる parameter を持つ検証用 model."""

    def __init__(self) -> None:
        super().__init__()
        self._used = nn.Linear(4, 2)
        self._unused = nn.Parameter(torch.zeros(2))

    @override
    def forward(self, values: Tensor) -> tuple[Tensor, Tensor]:
        projected = self._used(values)
        return projected, projected.sum(dim=1, keepdim=True)


class _SingleTensorModel(nn.Module):
    """タプルではなく Tensor を返す、契約違反の検証用 model."""

    def __init__(self) -> None:
        super().__init__()
        self._used = nn.Linear(4, 2)

    @override
    def forward(self, values: Tensor) -> Tensor:
        return self._used(values)


class _DriftingModel(nn.Module):
    """呼び出しのたびに出力が伸びる、eager と一致しない検証用 model.

    eager は 1 回目、compile 側は測定 pass が 2 回目なので出力が 2 倍になる。

    2 本目の出力は常に 0 で、両側 0 のときの相対差も同時に見られる。
    """

    def __init__(self) -> None:
        super().__init__()
        self._used = nn.Linear(4, 2)
        self._calls = 0

    @override
    def forward(self, values: Tensor) -> tuple[Tensor, Tensor]:
        self._calls += 1
        scaled = self._used(values) * float(self._calls)
        return scaled, torch.zeros_like(scaled)


class _FirstCallParameterModel(nn.Module):
    """1 回目の forward でだけ追加 parameter を使う検証用 model.

    eager 側にだけ ``_extra`` の勾配が付き、compile 側は ``None`` になる。
    """

    def __init__(self) -> None:
        super().__init__()
        self._used = nn.Linear(4, 2)
        self._extra = nn.Parameter(torch.ones(2))
        self._calls = 0

    @override
    def forward(self, values: Tensor) -> tuple[Tensor]:
        self._calls += 1
        projected = self._used(values)
        if self._calls == 1:
            projected = projected + self._extra
        return (projected,)


class _TimingModel(nn.Module):
    """時間計測だけに使う model.

    他のテストと compile cache を共有しないよう、この class 専用にする。
    """

    def __init__(self) -> None:
        super().__init__()
        self._used = nn.Linear(4, 2)

    @override
    def forward(self, values: Tensor) -> tuple[Tensor]:
        return (self._used(values).tanh(),)


def _sum_of_squares(outputs: tuple[Tensor, ...]) -> Tensor:
    return outputs[-1].square().mean()


def _first_output_sum_of_squares(outputs: tuple[Tensor, ...]) -> Tensor:
    return outputs[0].square().mean()


def _four_features(seed: int = 5) -> tuple[Tensor]:
    return (torch.randn((2, 4), generator=torch.Generator().manual_seed(seed)),)


class TestCompileOptions:
    """Compile 設定の検証."""

    def test_accepts_the_eager_options(self):
        assert EAGER_OPTIONS.validate() is None

    @pytest.mark.parametrize("field", ["backend", "mode"])
    def test_rejects_an_empty_name(self, field: str):
        options = attrs.evolve(EAGER_OPTIONS, **{field: ""})

        assert options.validate() == f"{field} は空にできません"


class TestParityTolerance:
    """許容誤差の検証."""

    def test_accepts_the_float32_defaults(self):
        assert FLOAT32_PARITY_TOLERANCES.validate() is None

    @pytest.mark.parametrize("relative", [-1e-6, float("nan"), float("inf")])
    def test_rejects_a_relative_tolerance_that_is_not_finite_and_non_negative(
        self, relative: float
    ):
        tolerance = ParityTolerance(relative=relative, absolute=0.0)

        reason = tolerance.validate()

        assert reason is not None
        assert reason.startswith("relative")

    def test_rejects_a_negative_absolute_tolerance(self):
        tolerance = ParityTolerance(relative=0.0, absolute=-1.0)

        assert tolerance.validate() == "absolute は 0 以上の有限値が必要です: -1.0"

    def test_reports_which_of_the_three_tolerances_is_invalid(self):
        tolerances = attrs.evolve(
            FLOAT32_PARITY_TOLERANCES,
            gradient=ParityTolerance(relative=0.0, absolute=-1.0),
        )

        reason = tolerances.validate()

        assert reason is not None
        assert reason.startswith("gradient: absolute")

    def test_accepts_zero_tolerance(self):
        exact = ParityTolerance(relative=0.0, absolute=0.0)

        assert (
            CompileParityTolerances(output=exact, loss=exact, gradient=exact).validate()
            is None
        )


class TestCompileParityResultVerdict:
    """``passed`` は 3 種類の差分と勾配の逸脱をすべて見る."""

    @staticmethod
    def _result(**overrides) -> CompileParityResult:
        matched = TensorDifference(
            maximum_absolute_difference=0.0,
            maximum_relative_difference=0.0,
            within_tolerance=True,
        )
        return attrs.evolve(
            CompileParityResult(
                outputs=(matched,),
                loss=matched,
                gradient=matched,
                checked_gradient_count=3,
                mismatched_gradient_parameters=(),
                missing_gradient_parameters=(),
                non_finite_gradient_parameters=(),
                eager_seconds=0.1,
                compiled_seconds=0.1,
                compile_setup_seconds=1.0,
            ),
            **overrides,
        )

    def test_passes_when_every_difference_is_within_tolerance(self):
        assert self._result().passed is True

    def test_fails_when_an_output_is_outside_the_tolerance(self):
        exceeded = TensorDifference(
            maximum_absolute_difference=1.0,
            maximum_relative_difference=1.0,
            within_tolerance=False,
        )

        assert self._result(outputs=(exceeded,)).passed is False

    def test_fails_when_the_loss_is_outside_the_tolerance(self):
        exceeded = TensorDifference(
            maximum_absolute_difference=1.0,
            maximum_relative_difference=1.0,
            within_tolerance=False,
        )

        assert self._result(loss=exceeded).passed is False

    @pytest.mark.parametrize(
        "field",
        [
            "mismatched_gradient_parameters",
            "missing_gradient_parameters",
            "non_finite_gradient_parameters",
        ],
    )
    def test_fails_when_a_parameter_is_listed_as_deviating(self, field: str):
        assert self._result(**{field: ("_used.weight",)}).passed is False


class TestCompileParityResultMeasure:
    """同一重みの eager 実行と compile 済み実行の突き合わせ."""

    def test_eager_backend_reproduces_outputs_loss_and_gradients_exactly(self):
        result = _compare(_regressor(), _inputs())

        assert result.passed is True
        assert result.loss.maximum_absolute_difference == 0.0
        assert result.gradient.maximum_absolute_difference == 0.0
        assert [
            difference.maximum_absolute_difference for difference in result.outputs
        ] == [0.0, 0.0]

    def test_reports_one_difference_per_model_output(self):
        result = _compare(_regressor(), _inputs())

        assert len(result.outputs) == 2

    def test_checks_every_parameter_that_receives_a_gradient(self):
        model = _regressor()

        result = _compare(model, _inputs())

        assert result.checked_gradient_count == len(list(model.parameters()))
        assert result.mismatched_gradient_parameters == ()
        assert result.missing_gradient_parameters == ()
        assert result.non_finite_gradient_parameters == ()

    def test_leaves_the_gradients_of_the_given_model_untouched(self):
        model = _regressor()

        _compare(model, _inputs())

        assert all(parameter.grad is None for parameter in model.parameters())

    def test_reports_timings_separately_from_the_first_compilation(self):
        started = time.perf_counter()
        result, reason = CompileParityResult.measure(
            _TimingModel(),
            _four_features(17),
            loss=_sum_of_squares,
            tolerances=FLOAT32_PARITY_TOLERANCES,
            options=EAGER_OPTIONS,
        )
        elapsed = time.perf_counter() - started

        assert reason is None
        assert result is not None
        # 3 つは呼び出し中の重ならない区間なので、合計が全体を超えることはない。
        # compile の費用が compiled_seconds へ混ざるとこの関係が壊れる
        assert (
            result.eager_seconds
            + result.compiled_seconds
            + result.compile_setup_seconds
            <= elapsed
        )
        # 初回 compile を含む区間だけが桁違いに長い。実測の比は process の
        # 状態で大きく変わる（このテスト単独なら 2,000 倍超、他テストで dynamo が
        # 温まった process では 26〜70 倍）ので、桁ではなく大小関係だけを見る
        assert result.compile_setup_seconds > result.compiled_seconds
        assert result.eager_seconds > 0.0
        assert result.compiled_seconds > 0.0

    def test_fullgraph_compilation_keeps_the_outputs_identical(self):
        result = _compare(
            _regressor(), _inputs(), CompileOptions(backend="eager", fullgraph=True)
        )

        assert result.passed is True

    def test_ignores_parameters_whose_gradient_is_none_on_both_sides(self):
        model = _UnusedParameterModel()
        generator = torch.Generator().manual_seed(5)

        result, reason = CompileParityResult.measure(
            model,
            (torch.randn((2, 4), generator=generator),),
            loss=_sum_of_squares,
            tolerances=FLOAT32_PARITY_TOLERANCES,
            options=EAGER_OPTIONS,
        )

        assert reason is None
        assert result is not None
        assert result.passed is True
        assert result.checked_gradient_count == 2
        assert result.missing_gradient_parameters == ()

    def test_reports_the_relative_difference_of_each_output(self):
        result, reason = CompileParityResult.measure(
            _DriftingModel(),
            _four_features(),
            loss=_first_output_sum_of_squares,
            tolerances=FLOAT32_PARITY_TOLERANCES,
            options=EAGER_OPTIONS,
        )

        assert reason is None
        assert result is not None
        assert result.passed is False
        # 期待値は「compiled_seconds は warm-up 後の 2 回目の pass」という
        # CompileParityResult の契約に依存する。compile 側の出力は 2 倍なので
        # |a - b| / max(|a|, |b|) は 0.5
        assert result.outputs[0].maximum_relative_difference == pytest.approx(0.5)
        assert result.outputs[0].maximum_absolute_difference > 0.0
        # 2 本目は両側とも 0 なので相対差も 0 とする
        assert result.outputs[1].maximum_absolute_difference == 0.0
        assert result.outputs[1].maximum_relative_difference == 0.0

    def test_lists_the_parameters_whose_gradients_disagree(self):
        result, reason = CompileParityResult.measure(
            _DriftingModel(),
            _four_features(),
            loss=_first_output_sum_of_squares,
            tolerances=FLOAT32_PARITY_TOLERANCES,
            options=EAGER_OPTIONS,
        )

        assert reason is None
        assert result is not None
        assert result.mismatched_gradient_parameters == ("_used.weight", "_used.bias")
        assert result.gradient.within_tolerance is False
        assert result.passed is False

    def test_lists_a_parameter_whose_gradient_is_missing_on_one_side(self):
        result, reason = CompileParityResult.measure(
            _FirstCallParameterModel(),
            _four_features(),
            loss=_sum_of_squares,
            tolerances=FLOAT32_PARITY_TOLERANCES,
            options=EAGER_OPTIONS,
        )

        assert reason is None
        assert result is not None
        # eager は 1 回目、compile 側の測定 pass は warm-up 後の 2 回目という
        # CompileParityResult の契約により、_extra の勾配は compile 側にだけ無い
        assert result.missing_gradient_parameters == ("_extra",)
        assert result.passed is False

    def test_rejects_options_that_do_not_validate(self):
        with pytest.raises(ValueError, match="backend は空"):
            CompileParityResult.measure(
                _regressor(),
                _inputs(),
                loss=_gaussian_loss,
                tolerances=FLOAT32_PARITY_TOLERANCES,
                options=CompileOptions(backend=""),
            )

    def test_returns_a_reason_when_the_backend_does_not_exist(self):
        result, reason = CompileParityResult.measure(
            _regressor(),
            _inputs(),
            loss=_gaussian_loss,
            tolerances=FLOAT32_PARITY_TOLERANCES,
            options=CompileOptions(backend="no_such_backend"),
        )

        assert result is None
        assert reason is not None
        assert "no_such_backend" in reason

    def test_rejects_an_empty_input_sequence(self):
        with pytest.raises(ValueError, match="inputs"):
            CompileParityResult.measure(
                _regressor(),
                (),
                loss=_gaussian_loss,
                tolerances=FLOAT32_PARITY_TOLERANCES,
                options=EAGER_OPTIONS,
            )

    def test_rejects_tolerances_that_do_not_validate(self):
        tolerances = attrs.evolve(
            FLOAT32_PARITY_TOLERANCES,
            output=ParityTolerance(relative=-1.0, absolute=0.0),
        )

        with pytest.raises(ValueError, match="output: relative"):
            CompileParityResult.measure(
                _regressor(),
                _inputs(),
                loss=_gaussian_loss,
                tolerances=tolerances,
                options=EAGER_OPTIONS,
            )

    def test_rejects_a_model_that_does_not_return_a_tuple(self):
        generator = torch.Generator().manual_seed(7)

        with pytest.raises(ValueError, match="Tensor のタプル"):
            CompileParityResult.measure(
                _SingleTensorModel(),
                (torch.randn((2, 4), generator=generator),),
                loss=_sum_of_squares,
                tolerances=FLOAT32_PARITY_TOLERANCES,
                options=EAGER_OPTIONS,
            )

    @skip_if_no_inductor
    def test_inductor_backend_matches_eager_within_float32_tolerances(self):
        result = _compare(
            _regressor(), _inputs(), CompileOptions(backend="inductor", fullgraph=True)
        )

        assert result.passed is True
        assert result.checked_gradient_count > 0
