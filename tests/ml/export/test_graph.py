"""ONNX graph の検査の公開契約.

計画 §4.6 / §6.3 に対応する。実 export した ONNX を実 ``onnx.checker`` へ通す。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import onnx
import pytest
from onnx import TensorProto, helper

from ml.export.graph import DEFAULT_DOMAIN, OnnxGraphSummary
from tests.ml.export import support

_OPSET = helper.make_opsetid("", support.OPSET_VERSION)
_LOCAL_DOMAIN = "local"
_VENDOR_DOMAIN = "com.example"


def _shared_summary() -> OnnxGraphSummary:
    summary, error = OnnxGraphSummary.inspect(support.shared_fp32_model_path())

    assert summary is not None, error
    return summary


def _value(name: str, dimensions: tuple[int, ...]) -> onnx.ValueInfoProto:
    return helper.make_tensor_value_info(name, TensorProto.FLOAT, list(dimensions))


def _finished_model(
    graph: onnx.GraphProto,
    *,
    opset_imports: Sequence[onnx.OperatorSetIdProto],
    functions: Sequence[onnx.FunctionProto] = (),
) -> onnx.ModelProto:
    model = helper.make_model(
        graph, opset_imports=list(opset_imports), functions=list(functions)
    )
    model.ir_version = onnx.IR_VERSION
    return model


def _shape_inference_failure_model() -> onnx.ModelProto:
    """素の checker は通るが shape 推論で broadcast できない graph."""

    graph = helper.make_graph(
        [helper.make_node("Add", ["left", "right"], ["sum"])],
        "broadcast_mismatch",
        [_value("left", (2, 3)), _value("right", (4, 5))],
        [_value("sum", (2, 3))],
    )
    return _finished_model(graph, opset_imports=[_OPSET])


def _branching_model() -> onnx.ModelProto:
    """``If`` の subgraph に node を隠した graph."""

    def branch(name: str, operator: str) -> onnx.GraphProto:
        return helper.make_graph(
            [helper.make_node(operator, ["left"], ["chosen"])],
            name,
            [],
            [_value("chosen", (2, 3))],
        )

    graph = helper.make_graph(
        [
            helper.make_node(
                "If",
                ["condition"],
                ["picked"],
                then_branch=branch("then_branch", "Neg"),
                else_branch=branch("else_branch", "Abs"),
            )
        ],
        "branching",
        [
            helper.make_tensor_value_info("condition", TensorProto.BOOL, []),
            _value("left", (2, 3)),
        ],
        [_value("picked", (2, 3))],
    )
    return _finished_model(graph, opset_imports=[_OPSET])


def _vendor_domain_model() -> onnx.ModelProto:
    """既定 domain の外にある operator を呼ぶ graph."""

    graph = helper.make_graph(
        [helper.make_node("VendorOp", ["left"], ["out"], domain=_VENDOR_DOMAIN)],
        "vendor",
        [_value("left", (2, 3))],
        [_value("out", (2, 3))],
    )
    return _finished_model(
        graph,
        opset_imports=[_OPSET, helper.make_opsetid(_VENDOR_DOMAIN, 1)],
    )


def _local_function_model() -> onnx.ModelProto:
    """既定 domain の node だけを持ち、local function を抱えた graph.

    function を呼ぶ node を置くとその node の domain が ``local`` になり、
    domain の検査が先に落ちる。function_names の検査だけを見るために、
    function は持たせるが呼ばない形にする。
    """

    function = helper.make_function(
        domain=_LOCAL_DOMAIN,
        fname="Twice",
        inputs=["value"],
        outputs=["doubled"],
        nodes=[helper.make_node("Add", ["value", "value"], ["doubled"])],
        opset_imports=[_OPSET],
    )
    graph = helper.make_graph(
        [helper.make_node("Add", ["left", "left"], ["sum"])],
        "carries_a_function",
        [_value("left", (2, 3))],
        [_value("sum", (2, 3))],
    )
    return _finished_model(
        graph,
        opset_imports=[_OPSET, helper.make_opsetid(_LOCAL_DOMAIN, 1)],
        functions=[function],
    )


class TestInspect:
    """ONNX ファイルを読んで要約する."""

    def test_summarizes_the_exported_model(self):
        summary = _shared_summary()

        assert summary.node_domains == ("",)
        assert summary.function_names == ()
        assert dict(summary.opset_versions) == {"": support.OPSET_VERSION}
        assert summary.default_opset_version == support.OPSET_VERSION
        assert summary.ir_version > 0
        assert "pytorch" in summary.producer

    def test_reports_the_input_and_output_tensors(self):
        summary = _shared_summary()

        assert tuple(tensor.name for tensor in summary.inputs) == support.INPUT_NAMES
        assert tuple(tensor.name for tensor in summary.outputs) == support.OUTPUT_NAMES
        assert summary.inputs[0].dimensions == (
            "1",
            str(support.IMAGE_CHANNELS),
            support.HEIGHT_SYMBOL,
            support.WIDTH_SYMBOL,
        )

    def test_lists_the_operator_types_actually_present(self):
        summary = _shared_summary()

        # GroupNorm は InstanceNormalization へ、pooling は ReduceMean へ分解される
        assert {"Conv", "Gemm", "InstanceNormalization", "ReduceMean"} <= set(
            summary.operator_types
        )

    def test_reports_a_path_that_does_not_exist(self, tmp_path: Path):
        summary, error = OnnxGraphSummary.inspect(tmp_path / "absent.onnx")

        assert summary is None
        assert error is not None
        # onnx.load の例外捕捉でも理由は返るので、不在そのものの検査まで見る
        assert "見つかりません" in error

    def test_reports_a_file_that_is_not_a_protocol_buffer(self, tmp_path: Path):
        broken = tmp_path / "broken.onnx"
        broken.write_bytes(b"not an onnx model at all")

        summary, error = OnnxGraphSummary.inspect(broken)

        assert summary is None
        assert error is not None

    def test_reports_a_json_document(self, tmp_path: Path):
        document = tmp_path / "model.onnx"
        document.write_text(json.dumps({"kind": "not-onnx"}), encoding="utf-8")

        summary, error = OnnxGraphSummary.inspect(document)

        assert summary is None
        assert error is not None

    def test_reports_an_output_name_that_collides_with_a_graph_value(
        self, tmp_path: Path
    ):
        # graph 内部に既に mean という値名があるので SSA 違反になる
        colliding = support.write_onnx_model(
            tmp_path / "colliding.onnx", output_names=("mean", "log_variance")
        )

        summary, error = OnnxGraphSummary.inspect(colliding)

        assert summary is None
        assert error is not None
        assert "mean" in error

    def test_lists_the_operators_hidden_inside_a_subgraph(self, tmp_path: Path):
        # If / Loop の subgraph へ独自 operator を隠されると検査が素通りする
        path = tmp_path / "branching.onnx"
        onnx.save(_branching_model(), str(path))

        summary, error = OnnxGraphSummary.inspect(path)

        assert summary is not None, error
        assert summary.operator_types == ("Abs", "If", "Neg")

    def test_reports_a_graph_whose_shapes_do_not_infer(self, tmp_path: Path):
        # 素の checker は通り、full_check の shape 推論だけが broadcast 不能を出す
        model = _shape_inference_failure_model()
        onnx.checker.check_model(model, full_check=False)
        path = tmp_path / "broadcast.onnx"
        onnx.save(model, str(path))

        summary, error = OnnxGraphSummary.inspect(path)

        assert summary is None
        assert error is not None


class TestVerifyStandardOperators:
    """独自 operator と local function を持ち込ませない."""

    def test_accepts_a_graph_whose_operators_are_all_allowed(self):
        summary = _shared_summary()

        assert (
            summary.verify_standard_operators(
                allowed_operator_types=set(summary.operator_types)
            )
            is None
        )

    def test_reports_operators_outside_the_allowlist(self):
        summary = _shared_summary()

        error = summary.verify_standard_operators(allowed_operator_types={"Conv"})

        assert error is not None
        assert "Gemm" in error

    def test_reports_a_graph_that_uses_a_vendor_domain(self, tmp_path: Path):
        path = tmp_path / "vendor.onnx"
        onnx.save(_vendor_domain_model(), str(path))
        summary, error = OnnxGraphSummary.inspect(path)

        assert summary is not None, error
        assert summary.node_domains == (_VENDOR_DOMAIN,)

        rejection = summary.verify_standard_operators(
            allowed_operator_types=set(summary.operator_types)
        )

        assert rejection is not None
        assert _VENDOR_DOMAIN in rejection

    def test_rejects_the_vendor_operators_that_ort_actually_emits(self, tmp_path: Path):
        # 裁定 2（最適化済み FP32 を artifact にしない）の回帰検出器。
        # x86 の ORT は com.microsoft.nchwc を出すが、domain 名は実装依存なので
        # 名前を固定せず「既定 domain 以外が現れる」ことだけを見る。
        optimized = support.write_ort_optimized_model(tmp_path / "optimized.onnx")
        summary, error = OnnxGraphSummary.inspect(optimized)

        assert summary is not None, error
        vendor_domains = sorted(set(summary.node_domains) - {DEFAULT_DOMAIN})
        if not vendor_domains:
            pytest.skip("この機体の ONNX Runtime は独自 domain の operator を出さない")

        rejection = summary.verify_standard_operators(
            allowed_operator_types=set(summary.operator_types)
        )

        assert rejection is not None
        assert vendor_domains[0] in rejection

    def test_reports_a_graph_that_carries_a_local_function(self, tmp_path: Path):
        path = tmp_path / "with_function.onnx"
        onnx.save(_local_function_model(), str(path))
        summary, error = OnnxGraphSummary.inspect(path)

        assert summary is not None, error
        # domain も operator も許可内なので、local function だけが理由になり得る
        assert summary.node_domains == ("",)
        assert summary.function_names == ("Twice",)

        rejection = summary.verify_standard_operators(
            allowed_operator_types=set(summary.operator_types)
        )

        assert rejection is not None
        assert "Twice" in rejection


class TestVerifyDynamicDimensions:
    """宣言した軸が symbol のまま残っているか."""

    def test_accepts_the_declared_symbols(self):
        summary = _shared_summary()

        assert (
            summary.verify_dynamic_dimensions(
                expected={
                    support.IMAGES_INPUT: {
                        2: support.HEIGHT_SYMBOL,
                        3: support.WIDTH_SYMBOL,
                    }
                }
            )
            is None
        )

    def test_reports_an_axis_that_became_a_fixed_value(self):
        summary = _shared_summary()

        error = summary.verify_dynamic_dimensions(
            expected={support.IMAGES_INPUT: {0: support.BATCH_SYMBOL}}
        )

        assert error is not None
        assert support.BATCH_SYMBOL in error
        # symbol 名の食い違いの理由も symbol を含むので、固定値へ落ちたこと自体を見る
        assert "特殊化" in error

    def test_reports_a_symbol_that_does_not_match_the_declaration(self):
        summary = _shared_summary()

        error = summary.verify_dynamic_dimensions(
            expected={support.IMAGES_INPUT: {2: "rows"}}
        )

        assert error is not None
        assert "rows" in error

    def test_reports_an_input_that_the_graph_does_not_have(self):
        summary = _shared_summary()

        error = summary.verify_dynamic_dimensions(
            expected={"absent_input": {0: support.BATCH_SYMBOL}}
        )

        assert error is not None
        assert "absent_input" in error

    def test_reports_an_axis_beyond_the_rank_of_the_input(self):
        summary = _shared_summary()

        error = summary.verify_dynamic_dimensions(
            expected={support.IMAGES_INPUT: {9: support.HEIGHT_SYMBOL}}
        )

        assert error is not None


class TestGraphTensor:
    """Graph の入出力 1 本を manifest の契約へ変換する."""

    def test_converts_an_input_into_a_tensor_contract(self):
        summary = _shared_summary()
        tensor = summary.inputs[0]

        contract = tensor.as_tensor_contract()

        assert contract.name == tensor.name
        assert contract.element_type == tensor.element_type
        assert contract.dimensions == tensor.dimensions
        assert contract.validate() is None
