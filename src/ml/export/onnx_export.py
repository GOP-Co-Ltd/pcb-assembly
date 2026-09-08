"""Eager model から ONNX を書き出す入口.

Export は黙って壊れる経路が多い。

``torch.compile`` 済み wrapper も train mode の module も例外にならず通り、
``dynamic_shapes`` の宣言も forward 側の書き方ひとつで無視される。

そこで入口で compile 済み wrapper を拒否し、複製を ``eval()`` してから
``torch.no_grad()`` の中で書き出す。

書き出したあとは checker と dynamic 軸の照合を必ず通す。

宣言した軸が固定値になっていたら、また opset が指定と違っていたら、成果物を
残さず理由を返す。

``opset_version`` に古い版を渡すと torch は黙って対応版へ引き上げるので、
書き出した graph の側で照合する。

重みは ONNX ファイルの中へ収め、外部データの sidecar を作らない。

package の manifest は model を 1 ファイルとして記録するため。

許可 operator の検査はここでは行わない。

許可集合はドメインの判断なので、呼び出し側が :class:`OnnxGraphSummary` へ聞く。
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from pathlib import Path

import attrs
import torch
from torch import Tensor, nn

from ml.export.graph import OnnxGraphSummary

DEFAULT_OPSET_VERSION = 20

# ``torch.compile`` が返す wrapper が、compile 前の module を持つ属性名。
_COMPILED_WRAPPER_ATTRIBUTE = "_orig_mod"


@attrs.frozen
class DynamicDimension:
    """ONNX 側で symbol のまま残したい 1 軸.

    ``input_name`` は forward の引数名と ONNX の入力名の両方を兼ねる。
    """

    input_name: str
    axis: int
    symbol: str

    def validate(self) -> str | None:
        """軸の指定が ONNX の dim_param として使える形かを検証する."""

        if not self.input_name:
            return "input_name は空にできません"
        if self.axis < 0:
            return f"axis は 0 以上が必要です: {self.input_name} の {self.axis}"
        if not self.symbol.isidentifier():
            return f"symbol は識別子が必要です: {self.symbol!r}"
        return None


@attrs.frozen
class OnnxExportOptions:
    """Export の入出力名と dynamic 軸.

    ``input_names`` は forward の引数と同じ順・同じ名前で並べる。

    ``dynamic_shapes`` を引数名で引くので、順序と綴りが契約になる。
    """

    input_names: tuple[str, ...]
    output_names: tuple[str, ...]
    dynamic_dimensions: tuple[DynamicDimension, ...] = ()
    opset_version: int = DEFAULT_OPSET_VERSION

    def validate(self) -> str | None:
        """入出力名と dynamic 軸の宣言だけで閉じる整合を検証する."""

        for label, names in (
            ("input_names", self.input_names),
            ("output_names", self.output_names),
        ):
            if not names:
                return f"{label} は 1 個以上が必要です"
            if any(not name for name in names):
                return f"{label} に空の名前があります"
            if len(set(names)) != len(names):
                return f"{label} が重複しています: {sorted(names)}"
        if shared := sorted(set(self.input_names) & set(self.output_names)):
            return f"入力名と出力名が衝突しています: {shared}"
        if self.opset_version < 1:
            return f"opset_version は正の整数が必要です: {self.opset_version}"
        for dimension in self.dynamic_dimensions:
            if error := dimension.validate():
                return error
            if dimension.input_name not in self.input_names:
                return (
                    "dynamic_dimensions の input_name が input_names に"
                    f"ありません: {dimension.input_name!r}"
                )
        return None

    def validate_for(self, example_inputs: Sequence[Tensor]) -> str | None:
        """Example 入力に対して dynamic 軸を宣言できるかを検証する.

        大きさ 1 以下の軸は宣言を認めない。

        ``torch.onnx.export`` は大きさ 1 の example からでも dynamic な ONNX を
        書き出せる（実測）。それでも拒否するのは、その example では「その軸が
        実際に変わる」ことを 1 度も確かめられないため。

        ``torch.export`` の ``Dim.AUTO`` 経路は大きさ 0 / 1 を特殊化するので、
        2 以上の example を要求する側へ倒す。
        """

        if len(example_inputs) != len(self.input_names):
            return (
                "example_inputs の個数が input_names と一致しません: "
                f"{len(example_inputs)} と {len(self.input_names)}"
            )
        by_name = dict(zip(self.input_names, example_inputs, strict=True))
        for dimension in self.dynamic_dimensions:
            tensor = by_name.get(dimension.input_name)
            if tensor is None:
                return (
                    "dynamic_dimensions の input_name が input_names に"
                    f"ありません: {dimension.input_name!r}"
                )
            if dimension.axis >= tensor.ndim:
                return (
                    f"入力 {dimension.input_name!r} に軸 {dimension.axis} が"
                    f"ありません: {tuple(tensor.shape)}"
                )
            size = int(tensor.shape[dimension.axis])
            if size <= 1:
                return (
                    f"大きさ {size} の軸は dynamic に宣言できません: "
                    f"{dimension.input_name!r} の軸 {dimension.axis}"
                    "（その example では軸が変わることを確かめられません）"
                )
        return None

    def as_dynamic_shapes(self) -> dict[str, dict[int, str]]:
        """``torch.onnx.export`` へ渡す dynamic_shapes を組む.

        宣言の無い入力にも空の辞書を置く。

        ``torch.export`` は dict 形式の dynamic_shapes に全 引数名を要求する。

        ``input_names`` に無い入力名の宣言も、例外にせずそのまま写像へ載せる。
        不整合は :meth:`validate` が理由文字列で返す担当とする。
        """

        shapes: dict[str, dict[int, str]] = {name: {} for name in self.input_names}
        for dimension in self.dynamic_dimensions:
            shapes.setdefault(dimension.input_name, {})[dimension.axis] = (
                dimension.symbol
            )
        return shapes


@attrs.frozen
class OnnxExportResult:
    """書き出して検査まで通った ONNX model."""

    model_path: Path
    summary: OnnxGraphSummary

    @classmethod
    def export(
        cls,
        model: nn.Module,
        example_inputs: Sequence[Tensor],
        model_path: Path,
        *,
        options: OnnxExportOptions,
    ) -> tuple[OnnxExportResult | None, str | None]:
        """複製した model を ONNX へ書き出し、graph を検査して返す.

        呼び出し側の model は複製するので、``training`` flag も重みも変わらない。

        失敗したときは書きかけの成果物を残さない。
        """

        destination = Path(model_path)
        if error := _reject_compiled_module(model):
            return None, error
        if error := options.validate():
            return None, error
        if error := options.validate_for(example_inputs):
            return None, error

        exported = copy.deepcopy(model).eval()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with torch.no_grad():
                torch.onnx.export(
                    exported,
                    tuple(example_inputs),
                    str(destination),
                    dynamo=True,
                    verbose=False,
                    external_data=False,
                    opset_version=options.opset_version,
                    input_names=list(options.input_names),
                    output_names=list(options.output_names),
                    dynamic_shapes=options.as_dynamic_shapes(),
                )
        except Exception as error:  # noqa: BLE001 - export の失敗は理由文字列にする
            return _discard(destination, f"ONNX export に失敗しました: {error}")

        summary, error = OnnxGraphSummary.inspect(destination)
        if summary is None:
            return _discard(destination, error)
        if error := summary.verify_dynamic_dimensions(
            expected=options.as_dynamic_shapes()
        ):
            return _discard(destination, error)
        if summary.default_opset_version != options.opset_version:
            return _discard(
                destination,
                "書き出した opset version が指定と一致しません: "
                f"{summary.default_opset_version}"
                f"（期待値 {options.opset_version}）",
            )
        return cls(model_path=destination, summary=summary), None


def _reject_compiled_module(model: nn.Module) -> str | None:
    """``torch.compile`` 済み wrapper を理由文字列で拒否する.

    compile 済み wrapper を渡しても export は例外にならず黙って通る。

    ``torch.compile`` が返す wrapper は、compile 前の module を
    ``_orig_mod`` に持つ。この属性は parameter を 1 つも持たない module でも
    現れるので、``state_dict()`` のキーを走査するより確実に判別できる。
    """

    if hasattr(model, _COMPILED_WRAPPER_ATTRIBUTE):
        return (
            "torch.compile 済みの module は渡せません。"
            f"{_COMPILED_WRAPPER_ATTRIBUTE!r} を持つ wrapper は checkpoint と "
            "export の契約から外れます。compile 前の module を渡してください"
        )
    return None


def _discard(model_path: Path, reason: str | None) -> tuple[None, str | None]:
    """書きかけの成果物を消してから理由を返す."""

    model_path.unlink(missing_ok=True)
    return None, reason


__all__ = [
    "DEFAULT_OPSET_VERSION",
    "DynamicDimension",
    "OnnxExportOptions",
    "OnnxExportResult",
]
