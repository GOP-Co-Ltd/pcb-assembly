"""書き出した ONNX と eager 実行の一致検証.

Export が通ることと、同じ数を返すことは別問題である。

decomposition や最適化で数値が変わっても graph の検査は素通りするので、
同じ入力を両方へ流して突き合わせる。

差分の計算は :class:`TensorDifference` と共有する。

compile parity と同じ尺度で語れるようにするため。

出力の符号と有限性も同時に見る。

「平均は正」のようなドメインの要求は、名前で受け取って ``ml`` 側は解釈しない。

この module は onnx を import しない。

Raspberry Pi 5 の ``ml-runtime`` 層で読めることを保ちたいため。
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from pathlib import Path

import attrs
import onnxruntime
import torch
from torch import Tensor, nn

from ml.evaluation.compile_parity import ParityTolerance, TensorDifference

# ``torch.compile`` が返す wrapper が、compile 前の module を持つ属性名。
_COMPILED_WRAPPER_ATTRIBUTE = "_orig_mod"
_CPU_PROVIDERS = ["CPUExecutionProvider"]


@attrs.frozen(eq=False)
class ParityCase:
    """1 回ぶんの比較入力.

    Tensor を持つので ``eq=False`` にする。

    attrs の既定 ``__eq__`` は Tensor 比較が bool にならず壊れるため。
    """

    case_id: str
    inputs: tuple[Tensor, ...]


@attrs.frozen
class CaseParity:
    """1 case ぶんの突き合わせ結果."""

    case_id: str
    differences: tuple[TensorDifference, ...]
    non_finite_output_names: tuple[str, ...]
    non_positive_output_names: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """差分が許容内で、非有限も非正も無いかを返す."""

        return (
            all(difference.within_tolerance for difference in self.differences)
            and self.non_finite_output_names == ()
            and self.non_positive_output_names == ()
        )


@attrs.frozen
class OnnxParityResult:
    """全 case ぶんの突き合わせ結果."""

    output_names: tuple[str, ...]
    cases: tuple[CaseParity, ...]

    @classmethod
    def measure(
        cls,
        model: nn.Module,
        onnx_model_path: Path,
        cases: Sequence[ParityCase],
        *,
        input_names: Sequence[str],
        output_names: Sequence[str],
        tolerance: ParityTolerance,
        positive_output_names: Sequence[str] = (),
    ) -> tuple[OnnxParityResult | None, str | None]:
        """同じ入力を eager と ONNX Runtime へ流し、出力の差を返す.

        呼び出し側の model は複製するので ``training`` flag も重みも変わらない。

        session の生成や実行が失敗したときは例外にせず理由を返す。
        """

        names = tuple(input_names)
        outputs = tuple(output_names)
        positive = tuple(positive_output_names)
        if error := _validate_arguments(
            model, cases, names, outputs, positive, tolerance
        ):
            return None, error

        try:
            session = onnxruntime.InferenceSession(
                str(onnx_model_path), providers=_CPU_PROVIDERS
            )
        except Exception as error:  # noqa: BLE001 - ORT の失敗は理由文字列にする
            return None, (
                "ONNX Runtime session を作れません: " f"{onnx_model_path}（{error}）"
            )

        if error := _verify_session_names(session, names, outputs):
            return None, error

        evaluated = copy.deepcopy(model).eval()
        measured: list[CaseParity] = []
        for case in cases:
            if len(case.inputs) != len(names):
                return None, (
                    f"case {case.case_id!r} の入力数が input_names と一致しません: "
                    f"{len(case.inputs)} と {len(names)}"
                )
            with torch.no_grad():
                eager = evaluated(*case.inputs)
            if not isinstance(eager, tuple) or len(eager) != len(outputs):
                return None, (
                    f"model の出力数が output_names と一致しません: "
                    f"case {case.case_id!r}"
                )
            feeds = {
                name: tensor.detach().cpu().numpy()
                for name, tensor in zip(names, case.inputs, strict=True)
            }
            try:
                produced = session.run(list(outputs), feeds)
            except Exception as error:  # noqa: BLE001 - ORT の失敗は理由文字列にする
                return None, f"case {case.case_id!r} の推論に失敗しました: {error}"
            measured.append(
                _case_parity(
                    case_id=case.case_id,
                    output_names=outputs,
                    positive_output_names=positive,
                    eager_outputs=eager,
                    onnx_outputs=tuple(torch.from_numpy(value) for value in produced),
                    tolerance=tolerance,
                )
            )
        return cls(output_names=outputs, cases=tuple(measured)), None

    @property
    def passed(self) -> bool:
        """すべての case が許容内に収まったかを返す."""

        return all(case.passed for case in self.cases)


def _validate_arguments(
    model: nn.Module,
    cases: Sequence[ParityCase],
    input_names: tuple[str, ...],
    output_names: tuple[str, ...],
    positive_output_names: tuple[str, ...],
    tolerance: ParityTolerance,
) -> str | None:
    if hasattr(model, _COMPILED_WRAPPER_ATTRIBUTE):
        return (
            "torch.compile 済みの module は渡せません。"
            f"{_COMPILED_WRAPPER_ATTRIBUTE!r} を持つ wrapper は checkpoint と "
            "export の契約から外れます。compile 前の module を渡してください"
        )
    if error := tolerance.validate():
        return error
    if not cases:
        return "cases は 1 件以上が必要です"
    if not input_names:
        return "input_names は 1 個以上が必要です"
    if not output_names:
        return "output_names は 1 個以上が必要です"
    if unknown := sorted(set(positive_output_names) - set(output_names)):
        return f"positive_output_names が output_names にありません: {unknown}"
    identifiers = [case.case_id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        return f"case_id が重複しています: {sorted(identifiers)}"
    return None


def _verify_session_names(
    session: onnxruntime.InferenceSession,
    input_names: tuple[str, ...],
    output_names: tuple[str, ...],
) -> str | None:
    actual_inputs = tuple(value.name for value in session.get_inputs())
    if actual_inputs != input_names:
        return (
            "input_names が ONNX model の入力と一致しません: "
            f"{list(input_names)}（model は {list(actual_inputs)}）"
        )
    actual_outputs = {value.name for value in session.get_outputs()}
    if missing := sorted(set(output_names) - actual_outputs):
        return (
            f"output_names が ONNX model の出力にありません: {missing}"
            f"（model は {sorted(actual_outputs)}）"
        )
    return None


def _case_parity(
    *,
    case_id: str,
    output_names: tuple[str, ...],
    positive_output_names: tuple[str, ...],
    eager_outputs: tuple[Tensor, ...],
    onnx_outputs: tuple[Tensor, ...],
    tolerance: ParityTolerance,
) -> CaseParity:
    differences: list[TensorDifference] = []
    non_finite: list[str] = []
    non_positive: list[str] = []
    for name, eager, actual in zip(
        output_names, eager_outputs, onnx_outputs, strict=True
    ):
        differences.append(
            TensorDifference.between(eager, actual.to(eager.dtype), tolerance=tolerance)
        )
        if not _all_finite(eager) or not _all_finite(actual):
            non_finite.append(name)
        if name in positive_output_names and (
            not _all_positive(eager) or not _all_positive(actual)
        ):
            non_positive.append(name)
    return CaseParity(
        case_id=case_id,
        differences=tuple(differences),
        non_finite_output_names=tuple(non_finite),
        non_positive_output_names=tuple(non_positive),
    )


def _all_finite(values: Tensor) -> bool:
    return bool(torch.isfinite(values).all().item())


def _all_positive(values: Tensor) -> bool:
    return bool((values > 0).all().item())


__all__ = [
    "CaseParity",
    "OnnxParityResult",
    "ParityCase",
]
