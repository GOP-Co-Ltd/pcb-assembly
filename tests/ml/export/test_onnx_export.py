"""Eager model から ONNX を書き出す入口の公開契約.

計画 §4.7 / §6.4 / 論点 4 に対応する。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast, override

import attrs
import numpy as np
import onnxruntime
import pytest
import torch
from torch import Tensor

from ml.export.onnx_export import (
    DEFAULT_OPSET_VERSION,
    DynamicDimension,
    OnnxExportOptions,
    OnnxExportResult,
)
from tests.ml.export import support

HEIGHT = DynamicDimension(
    input_name=support.IMAGES_INPUT, axis=2, symbol=support.HEIGHT_SYMBOL
)
WIDTH = DynamicDimension(
    input_name=support.IMAGES_INPUT, axis=3, symbol=support.WIDTH_SYMBOL
)
BATCH = DynamicDimension(
    input_name=support.IMAGES_INPUT, axis=0, symbol=support.BATCH_SYMBOL
)

OPTIONS = OnnxExportOptions(
    input_names=support.INPUT_NAMES,
    output_names=support.OUTPUT_NAMES,
    dynamic_dimensions=(HEIGHT, WIDTH),
)


class _ParameterFreeModel(torch.nn.Module):
    """Parameter を 1 つも持たない tiny model.

    ``state_dict()`` が空になるので、compile 済みかどうかを state_dict のキーの
    前置きからは判別できない。
    """

    @override
    def forward(self, images: Tensor, conditioning: Tensor) -> tuple[Tensor, Tensor]:
        """画像と条件変数から mean と log variance を返す."""

        mean = images.mean(dim=(1, 2, 3), keepdim=True).squeeze(-1).squeeze(-1)
        return mean, conditioning.sum(dim=1, keepdim=True)


def _export(
    model_path: Path,
    *,
    model: torch.nn.Module | None = None,
    example: Sequence[Tensor] | None = None,
    options: OnnxExportOptions = OPTIONS,
) -> OnnxExportResult:
    result, error = OnnxExportResult.export(
        support.build_tiny_model() if model is None else model,
        tuple(support.example_inputs()) if example is None else example,
        model_path,
        options=options,
    )

    assert result is not None, error
    return result


class TestExport:
    """出荷形の FP32 ONNX を書き出す."""

    def test_keeps_the_declared_axes_dynamic(self, tmp_path: Path):
        result = _export(tmp_path / support.MODEL_FILENAME)

        assert result.model_path == tmp_path / support.MODEL_FILENAME
        assert result.summary.inputs[0].dimensions == (
            "1",
            str(support.IMAGE_CHANNELS),
            support.HEIGHT_SYMBOL,
            support.WIDTH_SYMBOL,
        )

    def test_writes_a_single_self_contained_file(self, tmp_path: Path):
        # 既定の external_data=True は model.onnx.data を横に作る。manifest は
        # model_filename を 1 個しか持たないので、それでは package から重みが落ちる
        _export(tmp_path / support.MODEL_FILENAME)

        assert sorted(entry.name for entry in tmp_path.iterdir()) == [
            support.MODEL_FILENAME
        ]

    @pytest.mark.parametrize(("height", "width"), [(32, 32), (64, 48), (16, 80)])
    def test_runs_at_other_resolutions_through_onnxruntime(
        self, tmp_path: Path, height: int, width: int
    ):
        result = _export(tmp_path / support.MODEL_FILENAME)
        session = onnxruntime.InferenceSession(
            str(result.model_path), providers=["CPUExecutionProvider"]
        )

        outputs = session.run(
            None, support.input_values(height=height, width=width, seed=7)
        )

        assert [np.asarray(output).shape for output in outputs] == [(1, 1), (1, 1)]

    def test_declares_the_dynamic_axes_to_onnxruntime(self, tmp_path: Path):
        result = _export(tmp_path / support.MODEL_FILENAME)
        session = onnxruntime.InferenceSession(
            str(result.model_path), providers=["CPUExecutionProvider"]
        )

        images = next(
            entry
            for entry in session.get_inputs()
            if entry.name == support.IMAGES_INPUT
        )

        assert images.shape == [
            1,
            support.IMAGE_CHANNELS,
            support.HEIGHT_SYMBOL,
            support.WIDTH_SYMBOL,
        ]

    def test_honours_the_requested_opset_version(self, tmp_path: Path):
        result = _export(
            tmp_path / support.MODEL_FILENAME,
            options=attrs.evolve(OPTIONS, opset_version=18),
        )

        assert result.summary.default_opset_version == 18

    def test_rejects_a_batch_axis_the_example_cannot_demonstrate(self, tmp_path: Path):
        # torch は batch=1 の example からでも dynamic な ONNX を書けてしまう。
        # 「軸が変わることを確かめられない example」を通さないのは export の入口の役目
        result, error = OnnxExportResult.export(
            support.build_tiny_model(),
            tuple(support.example_inputs(batch=1)),
            tmp_path / support.MODEL_FILENAME,
            options=attrs.evolve(OPTIONS, dynamic_dimensions=(BATCH, HEIGHT, WIDTH)),
        )

        assert result is None
        assert error is not None
        assert "dynamic に宣言できません" in error
        assert list(tmp_path.iterdir()) == []

    def test_reports_an_opset_version_that_torch_silently_raised(self, tmp_path: Path):
        # 古い ONNX Runtime 向けに opset 17 を頼んでも、torch は黙って 18 を書く。
        # 宣言と成果物が食い違ったまま出荷されないよう graph 側で照合する
        result, error = OnnxExportResult.export(
            support.build_tiny_model(),
            tuple(support.example_inputs()),
            tmp_path / support.MODEL_FILENAME,
            options=attrs.evolve(OPTIONS, opset_version=17),
        )

        assert result is None
        assert error is not None
        assert "opset version が指定と一致しません" in error
        assert list(tmp_path.iterdir()) == []

    def test_exports_a_fully_static_model(self, tmp_path: Path):
        result = _export(
            tmp_path / support.MODEL_FILENAME,
            options=attrs.evolve(OPTIONS, dynamic_dimensions=()),
        )

        assert result.summary.inputs[0].dimensions == (
            "1",
            str(support.IMAGE_CHANNELS),
            str(support.IMAGE_SIZE),
            str(support.IMAGE_SIZE),
        )

    def test_does_not_change_the_callers_model(self, tmp_path: Path):
        model = support.build_tiny_model()
        model.train()
        before = {
            name: parameter.detach().clone()
            for name, parameter in model.named_parameters()
        }

        _export(tmp_path / support.MODEL_FILENAME, model=model)

        assert model.training is True
        assert all(
            torch.equal(before[name], parameter)
            for name, parameter in model.named_parameters()
        )

    def test_writes_the_graph_in_evaluation_mode(self, tmp_path: Path):
        # 学習 mode のまま export すると graph に Dropout node が残る
        model = support.DropoutModel()
        model.train()
        options = OnnxExportOptions(
            input_names=("values",),
            output_names=("prediction",),
            dynamic_dimensions=(
                DynamicDimension(
                    input_name="values", axis=0, symbol=support.BATCH_SYMBOL
                ),
            ),
        )

        result = _export(
            tmp_path / support.MODEL_FILENAME,
            model=model,
            example=(torch.zeros(4, 4),),
            options=options,
        )

        assert "Dropout" not in result.summary.operator_types

    def test_rejects_a_compiled_wrapper(self, tmp_path: Path):
        compiled = cast("torch.nn.Module", torch.compile(support.build_tiny_model()))

        result, error = OnnxExportResult.export(
            compiled,
            tuple(support.example_inputs()),
            tmp_path / support.MODEL_FILENAME,
            options=OPTIONS,
        )

        assert result is None
        assert error is not None
        # torch 側の失敗でも理由は返るので、入口の拒否が理由であることまで見る
        assert "torch.compile" in error
        assert list(tmp_path.iterdir()) == []

    def test_rejects_a_compiled_wrapper_without_parameters(self, tmp_path: Path):
        compiled = cast("torch.nn.Module", torch.compile(_ParameterFreeModel()))

        # parameter が無いと state_dict が空になり、キーの前置きでは判別できない
        assert compiled.state_dict() == {}

        result, error = OnnxExportResult.export(
            compiled,
            tuple(support.example_inputs()),
            tmp_path / support.MODEL_FILENAME,
            options=OPTIONS,
        )

        assert result is None
        assert error is not None
        assert "torch.compile" in error
        assert list(tmp_path.iterdir()) == []

    def test_discards_the_file_when_the_graph_fails_inspection(self, tmp_path: Path):
        # graph の内部に既に mean があるので SSA 違反になる。torch が書き終えた
        # あとで検査が落ちる、唯一の「成果物が残り得る」失敗経路。
        options = attrs.evolve(OPTIONS, output_names=("mean", "log_variance"))

        result, error = OnnxExportResult.export(
            support.build_tiny_model(),
            tuple(support.example_inputs()),
            tmp_path / support.MODEL_FILENAME,
            options=options,
        )

        assert result is None
        assert error is not None
        assert list(tmp_path.iterdir()) == []

    def test_reports_an_axis_that_cannot_be_made_dynamic(self, tmp_path: Path):
        # conditioning の特徴次元は head が config と照合するので dynamic にできない
        options = attrs.evolve(
            OPTIONS,
            dynamic_dimensions=(
                HEIGHT,
                WIDTH,
                DynamicDimension(
                    input_name=support.CONDITIONING_INPUT, axis=1, symbol="features"
                ),
            ),
        )

        result, error = OnnxExportResult.export(
            support.build_tiny_model(),
            tuple(support.example_inputs()),
            tmp_path / support.MODEL_FILENAME,
            options=options,
        )

        assert result is None
        assert error is not None
        assert list(tmp_path.iterdir()) == []

    def test_reports_malformed_options_without_writing_a_file(self, tmp_path: Path):
        result, error = OnnxExportResult.export(
            support.build_tiny_model(),
            tuple(support.example_inputs()),
            tmp_path / support.MODEL_FILENAME,
            options=attrs.evolve(OPTIONS, output_names=()),
        )

        assert result is None
        assert error is not None
        assert list(tmp_path.iterdir()) == []


class TestOptionsValidate:
    """入出力名と dynamic 軸の宣言そのものの整合."""

    def test_accepts_the_shipping_options(self):
        assert OPTIONS.validate() is None

    @pytest.mark.parametrize(
        "options",
        [
            pytest.param(attrs.evolve(OPTIONS, input_names=()), id="no-input-names"),
            pytest.param(attrs.evolve(OPTIONS, output_names=()), id="no-output-names"),
            pytest.param(
                attrs.evolve(OPTIONS, opset_version=0), id="non-positive-opset"
            ),
            pytest.param(
                attrs.evolve(OPTIONS, input_names=("images", "images")),
                id="duplicate-input-name",
            ),
            pytest.param(
                attrs.evolve(
                    OPTIONS, output_names=("predicted_mean", "predicted_mean")
                ),
                id="duplicate-output-name",
            ),
            pytest.param(
                attrs.evolve(OPTIONS, input_names=("images", "")),
                id="empty-input-name",
            ),
            pytest.param(
                attrs.evolve(
                    OPTIONS,
                    output_names=(support.IMAGES_INPUT, "predicted_log_variance"),
                ),
                id="input-and-output-names-cross",
            ),
            pytest.param(
                attrs.evolve(
                    OPTIONS,
                    dynamic_dimensions=(
                        attrs.evolve(HEIGHT, symbol="my height"),
                        WIDTH,
                    ),
                ),
                id="symbol-is-not-an-identifier",
            ),
            pytest.param(
                attrs.evolve(
                    OPTIONS,
                    dynamic_dimensions=(attrs.evolve(HEIGHT, symbol=""), WIDTH),
                ),
                id="empty-symbol",
            ),
            pytest.param(
                attrs.evolve(
                    OPTIONS,
                    dynamic_dimensions=(
                        attrs.evolve(HEIGHT, input_name="absent_input"),
                    ),
                ),
                id="unknown-input-name",
            ),
            pytest.param(
                attrs.evolve(
                    OPTIONS, dynamic_dimensions=(attrs.evolve(HEIGHT, axis=-1),)
                ),
                id="negative-axis",
            ),
        ],
    )
    def test_reports_malformed_options(self, options: OnnxExportOptions):
        assert options.validate() is not None

    def test_uses_the_default_opset_version(self):
        assert OPTIONS.opset_version == DEFAULT_OPSET_VERSION


class TestDynamicDimensionValidate:
    """1 軸の宣言そのものの検証.

    ``OnnxExportOptions.validate`` 経由だと後続の「input_names に無い」検査が
    先に発火しうるので、単体で呼んで 1 分岐ずつ見る。
    """

    def test_accepts_the_shipping_declaration(self):
        assert HEIGHT.validate() is None

    @pytest.mark.parametrize(
        "dimension",
        [
            pytest.param(attrs.evolve(HEIGHT, input_name=""), id="empty-input-name"),
            pytest.param(attrs.evolve(HEIGHT, axis=-1), id="negative-axis"),
            pytest.param(attrs.evolve(HEIGHT, symbol="not an identifier"), id="symbol"),
            pytest.param(attrs.evolve(HEIGHT, symbol=""), id="empty-symbol"),
        ],
    )
    def test_reports_a_malformed_declaration(self, dimension: DynamicDimension):
        assert dimension.validate() is not None


class TestOptionsValidateFor:
    """宣言と example 入力の突き合わせ."""

    def test_accepts_the_shipping_example(self):
        assert OPTIONS.validate_for(support.example_inputs()) is None

    def test_reports_a_mismatched_number_of_example_inputs(self):
        images, _ = support.example_inputs()

        assert OPTIONS.validate_for((images,)) is not None

    def test_reports_an_axis_beyond_the_rank_of_the_example(self):
        options = attrs.evolve(
            OPTIONS, dynamic_dimensions=(attrs.evolve(HEIGHT, axis=7),)
        )

        assert options.validate_for(support.example_inputs()) is not None

    def test_reports_an_axis_whose_example_size_is_one(self):
        # torch.onnx.export は大きさ 1 でも dynamic な ONNX を書けるが、その example
        # では軸が実際に変わることを確かめられないので宣言を認めない
        options = attrs.evolve(OPTIONS, dynamic_dimensions=(BATCH, HEIGHT, WIDTH))

        assert options.validate_for(support.example_inputs(batch=1)) is not None

    def test_reports_an_input_name_that_is_not_declared(self):
        # 単独で呼ばれても KeyError を投げず理由文字列で返す
        options = attrs.evolve(
            OPTIONS,
            dynamic_dimensions=(
                DynamicDimension(input_name="absent", axis=0, symbol="batch"),
            ),
        )

        error = options.validate_for(tuple(support.example_inputs()))

        assert error is not None
        assert "absent" in error

    def test_accepts_a_batch_axis_whose_example_size_is_two(self):
        options = attrs.evolve(OPTIONS, dynamic_dimensions=(BATCH, HEIGHT, WIDTH))

        assert options.validate_for(support.example_inputs(batch=2)) is None


class TestAsDynamicShapes:
    """``torch.export`` へ渡す dynamic_shapes の形."""

    def test_names_every_input_even_without_a_dynamic_axis(self):
        # dict 形式の dynamic_shapes は全引数名を要求する。欠けると torch が例外を投げる
        assert OPTIONS.as_dynamic_shapes() == {
            support.IMAGES_INPUT: {
                2: support.HEIGHT_SYMBOL,
                3: support.WIDTH_SYMBOL,
            },
            support.CONDITIONING_INPUT: {},
        }

    def test_leaves_every_input_empty_when_nothing_is_dynamic(self):
        options = attrs.evolve(OPTIONS, dynamic_dimensions=())

        assert options.as_dynamic_shapes() == {
            support.IMAGES_INPUT: {},
            support.CONDITIONING_INPUT: {},
        }

    def test_keeps_an_undeclared_input_name_instead_of_raising(self):
        # 不整合は validate() の担当。写像を組む側は例外にしない
        options = attrs.evolve(
            OPTIONS,
            dynamic_dimensions=(
                DynamicDimension(input_name="absent", axis=0, symbol="batch"),
            ),
        )

        assert options.as_dynamic_shapes()["absent"] == {0: "batch"}
        assert options.validate() is not None
