"""Eager 実行と ``torch.compile`` 実行の一致を確かめる比較器.

同じ重みの model を 2 つ用意し、片方だけ compile して forward 出力・loss・
勾配を突き合わせる。

compile の最適化が数値を壊していないことを、学習前に 1 度で確認するため。

target と sample weight は呼び出し側の loss クロージャが閉じ込める。

``ml`` は「model 出力タプル → 0 次元 loss」しか知らない。

``torch.compile`` 自体が失敗したときは例外にせず理由文字列を返す。

backend の可用性は環境依存であり、比較器の呼び出し側が扱うべき条件のため。
"""

from __future__ import annotations

import copy
import math
import time
from collections.abc import Callable, Sequence

import attrs
import torch
from torch import Tensor, nn


@attrs.frozen
class CompileOptions:
    """``torch.compile`` へ渡す設定."""

    backend: str = "inductor"
    mode: str = "default"
    fullgraph: bool = False
    dynamic: bool | None = None

    def validate(self) -> str | None:
        """Backend 名と mode 名が空でないことを検証する.

        名前が実在するかは環境依存なので、実行して理由文字列で返す。
        """

        for name in ("backend", "mode"):
            value: str = getattr(self, name)
            if not value:
                return f"{name} は空にできません"
        return None


@attrs.frozen
class ParityTolerance:
    """1 種類の値に許す相対誤差と絶対誤差.

    判定は ``torch.allclose`` と同じ ``|a - b| <= absolute + relative * |a|``。
    """

    relative: float
    absolute: float

    def validate(self) -> str | None:
        """許容誤差が有限かつ非負であることを検証する."""

        for name in ("relative", "absolute"):
            value: float = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                return f"{name} は 0 以上の有限値が必要です: {value}"
        return None


@attrs.frozen
class CompileParityTolerances:
    """出力・loss・勾配それぞれに許す誤差."""

    output: ParityTolerance
    loss: ParityTolerance
    gradient: ParityTolerance

    def validate(self) -> str | None:
        """3 種類の許容誤差をまとめて検証する."""

        for name in ("output", "loss", "gradient"):
            tolerance: ParityTolerance = getattr(self, name)
            if error := tolerance.validate():
                return f"{name}: {error}"
        return None


# float32 で学習する model の既定値。引数のデフォルトには置かず、呼び出し側が明示的に渡す
FLOAT32_PARITY_TOLERANCES = CompileParityTolerances(
    output=ParityTolerance(relative=1e-4, absolute=1e-6),
    loss=ParityTolerance(relative=1e-4, absolute=1e-6),
    gradient=ParityTolerance(relative=1e-3, absolute=1e-6),
)


@attrs.frozen
class TensorDifference:
    """2 つの tensor の食い違いの大きさ.

    相対差は ``|a - b| / max(|a|, |b|)`` で、両方 0 の要素は 0 とする。

    片方だけ 0 のときも 2 以下に収まり、報告値が無限大にならないため。
    """

    maximum_absolute_difference: float
    maximum_relative_difference: float
    within_tolerance: bool


@attrs.frozen
class CompileParityResult:
    """Eager と compile 済み実行を突き合わせた結果.

    ``eager_seconds`` は warm-up なしの 1 pass、``compiled_seconds`` は
    compile 済みの 2 回目の pass を測った値。

    初回 compile の費用は ``compile_setup_seconds`` に分けてある。
    """

    outputs: tuple[TensorDifference, ...]
    loss: TensorDifference
    gradient: TensorDifference
    checked_gradient_count: int
    mismatched_gradient_parameters: tuple[str, ...]
    missing_gradient_parameters: tuple[str, ...]
    non_finite_gradient_parameters: tuple[str, ...]
    eager_seconds: float
    compiled_seconds: float
    compile_setup_seconds: float

    @classmethod
    def measure(
        cls,
        model: nn.Module,
        inputs: Sequence[Tensor],
        *,
        loss: Callable[[tuple[Tensor, ...]], Tensor],
        tolerances: CompileParityTolerances,
        options: CompileOptions,
    ) -> tuple[CompileParityResult | None, str | None]:
        """同じ重みの model を eager と compile 済みで走らせ、差分を返す.

        model は 2 つ複製するので、呼び出し側の model と勾配は変わらない。

        device 解決はせず、model と入力が置かれている device のまま走らせる。

        ``torch.compile`` が失敗したときだけ ``(None, 理由)`` を返す。
        """

        if error := tolerances.validate():
            raise ValueError(error)
        if error := options.validate():
            raise ValueError(error)
        if len(inputs) == 0:
            raise ValueError("inputs は 1 個以上の Tensor が必要です")

        device = inputs[0].device
        eager_model = copy.deepcopy(model)
        compiled_source = copy.deepcopy(model)

        eager_outputs, eager_loss, eager_seconds = _run_pass(
            eager_model, eager_model, inputs, loss, device
        )

        setup_started = time.perf_counter()
        try:
            compiled_model = torch.compile(
                compiled_source,
                backend=options.backend,
                mode=options.mode,
                fullgraph=options.fullgraph,
                dynamic=options.dynamic,
            )
            _run_pass(compiled_model, compiled_source, inputs, loss, device)
        except Exception as exc:  # noqa: BLE001 - backend の可用性は呼び出し側の条件
            return None, (
                "compile 済み model の実行に失敗しました"
                f"（backend={options.backend}）: {exc}"
            )
        compile_setup_seconds = time.perf_counter() - setup_started

        compiled_outputs, compiled_loss, compiled_seconds = _run_pass(
            compiled_model, compiled_source, inputs, loss, device
        )

        gradients = _compare_gradients(
            eager_model, compiled_source, tolerances.gradient
        )
        return (
            cls(
                outputs=tuple(
                    _tensor_difference(eager, compiled, tolerances.output)
                    for eager, compiled in zip(
                        eager_outputs, compiled_outputs, strict=True
                    )
                ),
                loss=_tensor_difference(eager_loss, compiled_loss, tolerances.loss),
                gradient=gradients.difference,
                checked_gradient_count=gradients.checked_count,
                mismatched_gradient_parameters=gradients.mismatched,
                missing_gradient_parameters=gradients.missing,
                non_finite_gradient_parameters=gradients.non_finite,
                eager_seconds=eager_seconds,
                compiled_seconds=compiled_seconds,
                compile_setup_seconds=compile_setup_seconds,
            ),
            None,
        )

    @property
    def passed(self) -> bool:
        """すべての出力・loss・勾配が許容誤差に収まったかを返す."""

        return (
            all(difference.within_tolerance for difference in self.outputs)
            and self.loss.within_tolerance
            and self.gradient.within_tolerance
            and self.mismatched_gradient_parameters == ()
            and self.missing_gradient_parameters == ()
            and self.non_finite_gradient_parameters == ()
        )


@attrs.frozen
class _GradientComparison:
    difference: TensorDifference
    checked_count: int
    mismatched: tuple[str, ...]
    missing: tuple[str, ...]
    non_finite: tuple[str, ...]


def _run_pass(
    module: Callable[..., object],
    owner: nn.Module,
    inputs: Sequence[Tensor],
    loss: Callable[[tuple[Tensor, ...]], Tensor],
    device: torch.device,
) -> tuple[tuple[Tensor, ...], Tensor, float]:
    """Forward と backward を 1 往復させ、出力・loss・経過秒を返す.

    ``owner`` は勾配を持つ側の module で、compile 済み wrapper と区別する。
    """

    owner.zero_grad(set_to_none=True)
    _synchronize(device)
    started = time.perf_counter()
    outputs = _require_output_tuple(module(*inputs))
    loss_value = loss(outputs)
    loss_value.backward()
    _synchronize(device)
    return outputs, loss_value.detach(), time.perf_counter() - started


def _require_output_tuple(outputs: object) -> tuple[Tensor, ...]:
    if not isinstance(outputs, tuple) or not all(
        isinstance(output, Tensor) for output in outputs
    ):
        raise ValueError(f"model は Tensor のタプルを返す必要があります: {outputs!r}")
    return outputs


def _synchronize(device: torch.device) -> None:
    if device.type == "cuda":
        torch.cuda.synchronize(device)


def _tensor_difference(
    eager: Tensor, compiled: Tensor, tolerance: ParityTolerance
) -> TensorDifference:
    left = eager.detach().double()
    right = compiled.detach().double()
    absolute = (left - right).abs()
    scale = torch.maximum(left.abs(), right.abs())
    relative = torch.where(scale > 0, absolute / scale, torch.zeros_like(absolute))
    allowed = tolerance.absolute + tolerance.relative * left.abs()
    return TensorDifference(
        maximum_absolute_difference=_maximum(absolute),
        maximum_relative_difference=_maximum(relative),
        within_tolerance=bool((absolute <= allowed).all().item()),
    )


def _maximum(values: Tensor) -> float:
    if values.numel() == 0:
        return 0.0
    return float(values.max().item())


def _compare_gradients(
    eager_model: nn.Module, compiled_model: nn.Module, tolerance: ParityTolerance
) -> _GradientComparison:
    """名前ごとに勾配を突き合わせ、最大の食い違いと逸脱した parameter を返す.

    両方が ``None`` の勾配は不一致にしない。

    mask なしの forward で学習可能 padding pixel が使われないなど、正常に
    起こり得るため。
    """

    compiled_gradients = {
        name: parameter.grad for name, parameter in compiled_model.named_parameters()
    }
    differences: list[TensorDifference] = []
    mismatched: list[str] = []
    missing: list[str] = []
    non_finite: list[str] = []
    for name, parameter in eager_model.named_parameters():
        eager_gradient = parameter.grad
        compiled_gradient = compiled_gradients.get(name)
        if eager_gradient is None and compiled_gradient is None:
            continue
        if eager_gradient is None or compiled_gradient is None:
            missing.append(name)
            continue
        if not bool(torch.isfinite(eager_gradient).all().item()) or not bool(
            torch.isfinite(compiled_gradient).all().item()
        ):
            non_finite.append(name)
        difference = _tensor_difference(eager_gradient, compiled_gradient, tolerance)
        differences.append(difference)
        if not difference.within_tolerance:
            mismatched.append(name)
    return _GradientComparison(
        difference=TensorDifference(
            maximum_absolute_difference=max(
                (item.maximum_absolute_difference for item in differences), default=0.0
            ),
            maximum_relative_difference=max(
                (item.maximum_relative_difference for item in differences), default=0.0
            ),
            within_tolerance=all(item.within_tolerance for item in differences),
        ),
        checked_count=len(differences),
        mismatched=tuple(mismatched),
        missing=tuple(missing),
        non_finite=tuple(non_finite),
    )


__all__ = [
    "FLOAT32_PARITY_TOLERANCES",
    "CompileOptions",
    "CompileParityResult",
    "CompileParityTolerances",
    "ParityTolerance",
    "TensorDifference",
]
