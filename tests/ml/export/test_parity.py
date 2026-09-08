"""Eager 出力と ONNX Runtime 出力の突き合わせの公開契約.

計画 §4.5 / §6.6 に対応する。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast, override

import pytest
import torch

from ml.evaluation.compile_parity import ParityTolerance
from ml.export.parity import OnnxParityResult, ParityCase
from tests.ml.export import support

TOLERANCE = ParityTolerance(relative=1e-4, absolute=1e-6)

SIGNED_INPUT = "values"
POSITIVE_OUTPUT = "positive_output"
NEGATIVE_OUTPUT = "negative_output"
QUOTIENT_OUTPUT = "quotient"


class _ParameterFreeModel(torch.nn.Module):
    """Parameter を 1 つも持たない tiny model.

    ``state_dict()`` が空になるので、compile 済みかどうかを state_dict のキーの
    前置きからは判別できない。
    """

    @override
    def forward(
        self, images: torch.Tensor, conditioning: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """画像と条件変数から mean と log variance を返す."""

        mean = images.mean(dim=(1, 2, 3), keepdim=True).squeeze(-1).squeeze(-1)
        return mean, conditioning.sum(dim=1, keepdim=True)


def _cases() -> tuple[ParityCase, ...]:
    sizes = ((16, 16), (64, 64), (16, 64), (64, 16))
    return tuple(
        ParityCase(
            case_id=f"{height}x{width}",
            inputs=support.example_inputs(height=height, width=width, seed=index),
        )
        for index, (height, width) in enumerate(sizes)
    )


def _measure(
    *,
    model: torch.nn.Module | None = None,
    model_path: Path | None = None,
    cases: Sequence[ParityCase] | None = None,
    input_names: Sequence[str] = support.INPUT_NAMES,
    output_names: Sequence[str] = support.OUTPUT_NAMES,
    positive_output_names: Sequence[str] = (),
) -> OnnxParityResult:
    result, error = OnnxParityResult.measure(
        support.build_tiny_model() if model is None else model,
        support.shared_fp32_model_path() if model_path is None else model_path,
        _cases()[:1] if cases is None else cases,
        input_names=input_names,
        output_names=output_names,
        tolerance=TOLERANCE,
        positive_output_names=positive_output_names,
    )

    assert result is not None, error
    return result


def _reject(
    *,
    model: torch.nn.Module | None = None,
    model_path: Path | None = None,
    cases: Sequence[ParityCase] | None = None,
    input_names: Sequence[str] = support.INPUT_NAMES,
    output_names: Sequence[str] = support.OUTPUT_NAMES,
    tolerance: ParityTolerance = TOLERANCE,
    positive_output_names: Sequence[str] = (),
) -> str:
    result, error = OnnxParityResult.measure(
        support.build_tiny_model() if model is None else model,
        support.shared_fp32_model_path() if model_path is None else model_path,
        _cases()[:1] if cases is None else cases,
        input_names=input_names,
        output_names=output_names,
        tolerance=tolerance,
        positive_output_names=positive_output_names,
    )

    assert result is None
    assert error is not None
    return error


def _signed_model_path(directory: Path, *, non_finite: bool = False) -> Path:
    model = (
        support.NonFiniteOutputModel() if non_finite else support.SignedOutputsModel()
    )
    outputs = (QUOTIENT_OUTPUT,) if non_finite else (POSITIVE_OUTPUT, NEGATIVE_OUTPUT)
    return support.write_onnx_model(
        directory / "signed.onnx",
        model=model,
        example=(torch.ones(2, 4),),
        input_names=(SIGNED_INPUT,),
        output_names=outputs,
        dynamic_shapes={SIGNED_INPUT: {0: support.BATCH_SYMBOL}},
    )


class TestMeasure:
    """Eager と ONNX Runtime の出力を case ごとに比べる."""

    def test_matches_the_exported_model_within_tolerance(self):
        result = _measure()

        assert result.output_names == support.OUTPUT_NAMES
        assert result.passed is True
        assert result.cases[0].passed is True

    def test_measures_every_case_that_was_given(self):
        cases = _cases()

        result = _measure(cases=cases)

        assert tuple(case.case_id for case in result.cases) == tuple(
            case.case_id for case in cases
        )
        assert result.passed is True

    def test_reports_a_mismatch_when_the_weights_differ(self):
        # 共有 ONNX は seed 0 の重み。別 seed の model と比べれば必ず食い違う
        result = _measure(model=support.build_tiny_model(seed=1))

        assert result.passed is False
        assert result.cases[0].differences[0].maximum_absolute_difference > 0.0
        assert result.cases[0].differences[0].within_tolerance is False

    def test_lists_no_violation_when_the_positive_output_is_positive(
        self, tmp_path: Path
    ):
        result = _measure(
            model=support.SignedOutputsModel(),
            model_path=_signed_model_path(tmp_path),
            cases=(ParityCase(case_id="ones", inputs=(torch.ones(2, 4),)),),
            input_names=(SIGNED_INPUT,),
            output_names=(POSITIVE_OUTPUT, NEGATIVE_OUTPUT),
            positive_output_names=(POSITIVE_OUTPUT,),
        )

        assert result.cases[0].non_positive_output_names == ()
        assert result.passed is True

    def test_lists_an_output_that_is_required_to_be_positive_but_is_not(
        self, tmp_path: Path
    ):
        result = _measure(
            model=support.SignedOutputsModel(),
            model_path=_signed_model_path(tmp_path),
            cases=(ParityCase(case_id="ones", inputs=(torch.ones(2, 4),)),),
            input_names=(SIGNED_INPUT,),
            output_names=(POSITIVE_OUTPUT, NEGATIVE_OUTPUT),
            positive_output_names=(NEGATIVE_OUTPUT,),
        )

        assert result.cases[0].non_positive_output_names == (NEGATIVE_OUTPUT,)
        assert result.passed is False

    def test_lists_a_non_finite_output(self, tmp_path: Path):
        result = _measure(
            model=support.NonFiniteOutputModel(),
            model_path=_signed_model_path(tmp_path, non_finite=True),
            cases=(ParityCase(case_id="ones", inputs=(torch.ones(2, 4),)),),
            input_names=(SIGNED_INPUT,),
            output_names=(QUOTIENT_OUTPUT,),
        )

        assert result.cases[0].non_finite_output_names == (QUOTIENT_OUTPUT,)
        assert result.passed is False

    def test_does_not_change_the_callers_model(self):
        model = support.build_tiny_model()
        model.train()

        _measure(model=model)

        assert model.training is True

    def test_rejects_a_compiled_wrapper(self):
        result, error = OnnxParityResult.measure(
            cast("torch.nn.Module", torch.compile(support.build_tiny_model())),
            support.shared_fp32_model_path(),
            _cases()[:1],
            input_names=support.INPUT_NAMES,
            output_names=support.OUTPUT_NAMES,
            tolerance=TOLERANCE,
        )

        assert result is None
        assert error is not None
        assert "torch.compile" in error

    def test_rejects_a_compiled_wrapper_without_parameters(self):
        compiled = cast("torch.nn.Module", torch.compile(_ParameterFreeModel()))

        # parameter が無いと state_dict が空になり、キーの前置きでは判別できない
        assert compiled.state_dict() == {}

        result, error = OnnxParityResult.measure(
            compiled,
            support.shared_fp32_model_path(),
            _cases()[:1],
            input_names=support.INPUT_NAMES,
            output_names=support.OUTPUT_NAMES,
            tolerance=TOLERANCE,
        )

        assert result is None
        assert error is not None
        assert "torch.compile" in error

    # ORT の session 生成と run はどんな崩れ方でも理由文字列を返すので、
    # 引数検証の各分岐は理由文まで見ないと後段に隠される。

    def test_reports_input_names_that_the_session_does_not_have(self):
        error = _reject(input_names=("pixels", "conditioning"))

        assert "ONNX model の入力と一致しません" in error

    def test_reports_output_names_that_the_session_does_not_have(self):
        error = _reject(output_names=("mean", "log_variance"))

        assert "ONNX model の出力にありません" in error

    def test_reports_an_empty_case_list(self):
        error = _reject(cases=())

        assert "cases は 1 件以上が必要です" in error

    def test_reports_an_empty_input_name_list(self):
        error = _reject(input_names=())

        assert "input_names は 1 個以上が必要です" in error

    def test_reports_an_empty_output_name_list(self):
        error = _reject(output_names=())

        assert "output_names は 1 個以上が必要です" in error

    def test_reports_a_positive_output_name_that_is_not_an_output(self):
        # 「平均は正」のようなドメイン要求を綴り間違えると、検査されないまま
        # parity が通ってしまう。output_names に無い名前は入口で弾く
        error = _reject(positive_output_names=("predicted_average",))

        assert "positive_output_names が output_names にありません" in error
        assert "predicted_average" in error

    def test_reports_a_duplicate_case_id(self):
        error = _reject(cases=(_cases()[0], _cases()[0]))

        assert "case_id が重複しています" in error

    def test_reports_a_malformed_tolerance(self):
        error = _reject(tolerance=ParityTolerance(relative=-1.0, absolute=1e-6))

        assert error

    def test_reports_a_case_whose_input_count_does_not_match(self):
        error = _reject(
            cases=(ParityCase(case_id="short", inputs=(torch.zeros(1, 6, 32, 32),)),)
        )

        assert "入力数が input_names と一致しません" in error

    def test_reports_a_model_whose_output_count_does_not_match(self, tmp_path: Path):
        # 1 出力の model を 2 出力として比べようとする
        error = _reject(
            model=support.SignedOutputsModel(),
            model_path=_signed_model_path(tmp_path),
            cases=(ParityCase(case_id="ones", inputs=(torch.ones(2, 4),)),),
            input_names=(SIGNED_INPUT,),
            output_names=(POSITIVE_OUTPUT,),
        )

        assert "出力数が output_names と一致しません" in error

    def test_reports_a_model_path_that_does_not_exist(self, tmp_path: Path):
        result, error = OnnxParityResult.measure(
            support.build_tiny_model(),
            tmp_path / "absent.onnx",
            _cases()[:1],
            input_names=support.INPUT_NAMES,
            output_names=support.OUTPUT_NAMES,
            tolerance=TOLERANCE,
        )

        assert result is None
        assert error is not None
