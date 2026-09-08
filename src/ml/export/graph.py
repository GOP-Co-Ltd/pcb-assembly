"""書き出した ONNX graph の検査.

Export は成功しても、graph が出荷できる形とは限らない。

値名の衝突・独自 domain の operator・宣言が無視された dynamic 軸は、いずれも
実行時まで表に出ない。

読み込みと同時に ``onnx.checker`` を ``full_check`` で通す。

SSA 違反のように model を書いた側では気付けない不整合を、生成直後に捕まえる。

許可する operator の集合はここに持たない。

実際の operator は model 構成で変わるので、集合はドメイン側の判断とする。
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from pathlib import Path
from types import MappingProxyType

import attrs
import onnx

from ml.export.manifest import TensorContract

DEFAULT_DOMAIN = ""


def _read_only_opset_versions(versions: Mapping[str, int]) -> Mapping[str, int]:
    """呼び出し側の辞書と切り離した、変更できない写像を返す."""

    return MappingProxyType(dict(versions))


@attrs.frozen
class GraphTensor:
    """Graph の入出力 1 本ぶんの名前・要素型・軸.

    軸は dynamic なら symbol 名、固定なら 10 進表記の文字列で持つ。
    """

    name: str
    element_type: str
    dimensions: tuple[str, ...]

    def as_tensor_contract(self) -> TensorContract:
        """同じ内容を manifest 側の型へ移す."""

        return TensorContract(
            name=self.name,
            element_type=self.element_type,
            dimensions=self.dimensions,
        )


@attrs.frozen
class OnnxGraphSummary:
    """検査済み ONNX model の要約.

    ``operator_types`` と ``node_domains`` は subgraph の node も含む。

    ``If`` や ``Loop`` の中へ独自 operator を隠されると検査が素通りするため。
    """

    ir_version: int
    producer: str
    opset_versions: Mapping[str, int] = attrs.field(converter=_read_only_opset_versions)
    node_domains: tuple[str, ...]
    operator_types: tuple[str, ...]
    function_names: tuple[str, ...]
    inputs: tuple[GraphTensor, ...]
    outputs: tuple[GraphTensor, ...]

    @classmethod
    def inspect(cls, model_path: Path) -> tuple[OnnxGraphSummary | None, str | None]:
        """ONNX model を読み、checker を通してから要約を返す.

        ``onnx`` が投げる例外はすべて理由文字列に変換する。
        """

        path = Path(model_path)
        if not path.is_file():
            return None, f"ONNX model が見つかりません: {path}"
        try:
            model = onnx.load(str(path))
        except Exception as error:  # noqa: BLE001 - onnx の失敗は理由文字列にする
            return None, f"ONNX model を読めません: {path}（{error}）"
        try:
            onnx.checker.check_model(model, full_check=True)
        except Exception as error:  # noqa: BLE001 - checker の失敗は理由文字列にする
            return None, f"ONNX model が検査を通りません: {path}（{error}）"
        nodes = tuple(_walk_nodes(model.graph))
        return (
            cls(
                ir_version=int(model.ir_version),
                producer=" ".join(
                    part
                    for part in (model.producer_name, model.producer_version)
                    if part
                ),
                opset_versions={
                    entry.domain: int(entry.version) for entry in model.opset_import
                },
                node_domains=tuple(sorted({node.domain for node in nodes})),
                operator_types=tuple(sorted({node.op_type for node in nodes})),
                function_names=tuple(
                    sorted(function.name for function in model.functions)
                ),
                inputs=tuple(_graph_tensor(value) for value in model.graph.input),
                outputs=tuple(_graph_tensor(value) for value in model.graph.output),
            ),
            None,
        )

    def verify_standard_operators(
        self, *, allowed_operator_types: Set[str]
    ) -> str | None:
        """独自 domain・local function・許可外 operator が無いかを検証する."""

        if foreign := sorted(set(self.node_domains) - {DEFAULT_DOMAIN}):
            return f"既定 domain 以外の operator を含んでいます: {foreign}"
        if self.function_names:
            return f"local function を含んでいます: {list(self.function_names)}"
        if disallowed := sorted(set(self.operator_types) - set(allowed_operator_types)):
            return f"許可されていない operator を含んでいます: {disallowed}"
        return None

    def verify_dynamic_dimensions(
        self, *, expected: Mapping[str, Mapping[int, str]]
    ) -> str | None:
        """宣言した軸が symbol のまま残っているかを検証する.

        固定値へ特殊化された軸と、symbol 名が食い違う軸を理由にする。
        """

        by_name = {tensor.name: tensor for tensor in self.inputs}
        for input_name, axes in expected.items():
            tensor = by_name.get(input_name)
            if tensor is None:
                return (
                    f"入力 {input_name!r} が graph にありません: " f"{sorted(by_name)}"
                )
            for axis, symbol in axes.items():
                if not 0 <= axis < len(tensor.dimensions):
                    return (
                        f"入力 {input_name!r} に軸 {axis} がありません: "
                        f"{list(tensor.dimensions)}"
                    )
                actual = tensor.dimensions[axis]
                if actual.isdigit():
                    return (
                        f"入力 {input_name!r} の軸 {axis} が固定値へ特殊化"
                        f"されています: {actual}（期待する symbol {symbol!r}）"
                    )
                if actual != symbol:
                    return (
                        f"入力 {input_name!r} の軸 {axis} の symbol が違います: "
                        f"{actual!r}（期待値 {symbol!r}）"
                    )
        return None

    @property
    def default_opset_version(self) -> int:
        """既定 domain の opset version.

        宣言が無ければ 0 を返す。
        """

        return self.opset_versions.get(DEFAULT_DOMAIN, 0)


def _walk_nodes(graph: onnx.GraphProto) -> list[onnx.NodeProto]:
    nodes: list[onnx.NodeProto] = []
    for node in graph.node:
        nodes.append(node)
        for attribute in node.attribute:
            if attribute.HasField("g"):
                nodes.extend(_walk_nodes(attribute.g))
            for subgraph in attribute.graphs:
                nodes.extend(_walk_nodes(subgraph))
    return nodes


def _graph_tensor(value: onnx.ValueInfoProto) -> GraphTensor:
    tensor_type = value.type.tensor_type
    return GraphTensor(
        name=value.name,
        element_type=onnx.TensorProto.DataType.Name(tensor_type.elem_type),
        dimensions=tuple(
            dimension.dim_param or str(dimension.dim_value)
            for dimension in tensor_type.shape.dim
        ),
    )


__all__ = [
    "DEFAULT_DOMAIN",
    "GraphTensor",
    "OnnxGraphSummary",
]
