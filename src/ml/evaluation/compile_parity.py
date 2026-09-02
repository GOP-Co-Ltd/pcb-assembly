"""PyTorch eager/compiled forward・loss・gradient parity mechanics."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

import torch
from torch import Tensor, nn

type GaussianLoss = Callable[[Tensor, Tensor, Tensor, Tensor], Tensor]


@dataclass(frozen=True)
class CompileStepResult:
    mean: Tensor
    log_variance: Tensor
    loss: Tensor
    gradients: Mapping[str, Tensor | None]
    seconds: float


@dataclass(frozen=True)
class TensorDifference:
    max_abs: float
    max_relative: float
    close: bool


@dataclass(frozen=True)
class CompileStepComparison:
    mean: TensorDifference
    log_variance: TensorDifference
    loss: TensorDifference
    gradient_max_abs: float
    gradient_max_relative: float
    checked_gradient_count: int
    missing_gradient_parameters: tuple[str, ...]
    nonfinite_gradient_parameters: tuple[str, ...]
    mismatched_gradient_parameters: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return (
            self.mean.close
            and self.log_variance.close
            and self.loss.close
            and not self.missing_gradient_parameters
            and not self.nonfinite_gradient_parameters
            and not self.mismatched_gradient_parameters
        )


@dataclass(frozen=True)
class PreparedCompileModels:
    eager_model: nn.Module
    compiled_source: nn.Module
    compiled_model: nn.Module | None
    trainable_parameter_count: int
    compile_setup_seconds: float
    graph_break_baseline: int
    compile_error: Exception | None


def resolve_compile_device(requested: str) -> torch.device:
    """Auto/cpu/cuda指定を実行可能なtorch deviceへ解決する."""

    if requested == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device = torch.device(requested)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested for compile parity but is unavailable")
    return device


def synchronize_device(device: torch.device) -> None:
    """計測境界でCUDAだけを同期する."""

    if device.type == "cuda":
        torch.cuda.synchronize(device)


def prepare_compile_models(
    model: nn.Module,
    *,
    device: torch.device,
    backend: str,
    mode: str,
    fullgraph: bool,
    dynamic: bool | None,
) -> PreparedCompileModels:
    """同一weightのeager/compiled model pairを準備する."""

    eager_model = copy.deepcopy(model).to(device=device, dtype=torch.float32).eval()
    compiled_source = copy.deepcopy(model).to(device=device, dtype=torch.float32).eval()
    trainable_parameter_count = sum(
        1 for parameter in eager_model.parameters() if parameter.requires_grad
    )
    compiled_parameter_count = sum(
        1 for parameter in compiled_source.parameters() if parameter.requires_grad
    )
    if trainable_parameter_count < 1:
        raise ValueError("compile parity model must have trainable parameters")
    if trainable_parameter_count != compiled_parameter_count:
        raise ValueError(
            "eager and compiled model copies differ in trainable parameters"
        )
    eager_state = eager_model.state_dict()
    compiled_state = compiled_source.state_dict()
    if eager_state.keys() != compiled_state.keys() or any(
        not torch.equal(eager_state[name], compiled_state[name]) for name in eager_state
    ):
        raise ValueError(
            "eager and compiled model copies do not have identical weights"
        )

    graph_break_baseline = torch_compile_graph_break_count()
    compile_started = time.perf_counter()
    compile_error: Exception | None = None
    compiled_model: nn.Module | None = None
    try:
        compiled_model = cast(
            nn.Module,
            torch.compile(
                compiled_source,
                backend=backend,
                mode=mode,
                fullgraph=fullgraph,
                dynamic=dynamic,
            ),
        )
    except Exception as error:
        compile_error = error
    return PreparedCompileModels(
        eager_model=eager_model,
        compiled_source=compiled_source,
        compiled_model=compiled_model,
        trainable_parameter_count=trainable_parameter_count,
        compile_setup_seconds=max(time.perf_counter() - compile_started, 0.0),
        graph_break_baseline=graph_break_baseline,
        compile_error=compile_error,
    )


def execute_compile_step(
    model: nn.Module,
    model_inputs: tuple[Tensor, ...],
    *,
    target: Tensor,
    sample_weight: Tensor,
    loss_function: GaussianLoss,
    device: torch.device,
    gradient_model: nn.Module | None = None,
) -> CompileStepResult:
    """1回のforward、Gaussian loss、backwardを計測する."""

    model.zero_grad(set_to_none=True)
    synchronize_device(device)
    started = time.perf_counter()
    output = model(*model_inputs)
    if not isinstance(output, tuple) or len(output) != 2:
        raise ValueError("Gaussian regression model must return (mean, log_variance)")
    mean, log_variance = output
    if not isinstance(mean, Tensor) or not isinstance(log_variance, Tensor):
        raise ValueError("Gaussian regression model outputs must be tensors")
    if mean.shape != target.shape or log_variance.shape != target.shape:
        raise ValueError("Gaussian regression model outputs must match target shape")
    loss = loss_function(mean, log_variance, target, sample_weight)
    loss.backward()
    synchronize_device(device)
    elapsed = max(time.perf_counter() - started, float.fromhex("0x1p-52"))
    gradients: dict[str, Tensor | None] = {}
    parameter_source = gradient_model or model
    for name, parameter in parameter_source.named_parameters():
        if parameter.requires_grad:
            gradients[name] = (
                None if parameter.grad is None else parameter.grad.detach().clone()
            )
    return CompileStepResult(
        mean=mean.detach().clone(),
        log_variance=log_variance.detach().clone(),
        loss=loss.detach().clone(),
        gradients=gradients,
        seconds=elapsed,
    )


def tensor_difference(
    eager: Tensor,
    compiled: Tensor,
    *,
    rtol: float,
    atol: float,
) -> TensorDifference:
    """2 tensorの最大誤差とtorch tolerance判定を返す."""

    if eager.shape != compiled.shape:
        raise ValueError("eager and compiled tensor shapes differ")
    if (
        not torch.all(torch.isfinite(eager)).item()
        or not torch.all(torch.isfinite(compiled)).item()
    ):
        raise ValueError("eager or compiled result contains non-finite values")
    absolute = torch.abs(eager - compiled)
    denominator = torch.maximum(torch.abs(eager), torch.abs(compiled))
    relative = torch.where(
        denominator > 0,
        absolute / denominator,
        torch.zeros_like(absolute),
    )
    return TensorDifference(
        max_abs=float(absolute.max().item()),
        max_relative=float(relative.max().item()),
        close=bool(torch.allclose(eager, compiled, rtol=rtol, atol=atol)),
    )


def compare_compile_steps(
    eager: CompileStepResult,
    compiled: CompileStepResult,
    *,
    rtol: float,
    atol: float,
) -> CompileStepComparison:
    """Forward、loss、全trainable gradientを比較する."""

    mean = tensor_difference(eager.mean, compiled.mean, rtol=rtol, atol=atol)
    log_variance = tensor_difference(
        eager.log_variance, compiled.log_variance, rtol=rtol, atol=atol
    )
    loss = tensor_difference(eager.loss, compiled.loss, rtol=rtol, atol=atol)
    parameter_names = tuple(sorted(set(eager.gradients) | set(compiled.gradients)))
    missing: list[str] = []
    nonfinite: list[str] = []
    mismatched: list[str] = []
    checked = 0
    max_abs = 0.0
    max_relative = 0.0
    for name in parameter_names:
        eager_gradient = eager.gradients.get(name)
        compiled_gradient = compiled.gradients.get(name)
        if eager_gradient is None or compiled_gradient is None:
            missing.append(name)
            continue
        if (
            not torch.all(torch.isfinite(eager_gradient)).item()
            or not torch.all(torch.isfinite(compiled_gradient)).item()
        ):
            nonfinite.append(name)
            continue
        checked += 1
        difference = tensor_difference(
            eager_gradient, compiled_gradient, rtol=rtol, atol=atol
        )
        max_abs = max(max_abs, difference.max_abs)
        max_relative = max(max_relative, difference.max_relative)
        if not difference.close:
            mismatched.append(name)
    return CompileStepComparison(
        mean=mean,
        log_variance=log_variance,
        loss=loss,
        gradient_max_abs=max_abs,
        gradient_max_relative=max_relative,
        checked_gradient_count=checked,
        missing_gradient_parameters=tuple(missing),
        nonfinite_gradient_parameters=tuple(nonfinite),
        mismatched_gradient_parameters=tuple(mismatched),
    )


def torch_compile_graph_break_count() -> int:
    """PyTorch Dynamoのprocess-wide累積graph-break数を返す."""

    from torch._dynamo.utils import counters

    graph_breaks = counters.get("graph_break", {})
    return sum(int(value) for value in graph_breaks.values())


__all__ = [
    "CompileStepComparison",
    "CompileStepResult",
    "GaussianLoss",
    "PreparedCompileModels",
    "TensorDifference",
    "compare_compile_steps",
    "execute_compile_step",
    "prepare_compile_models",
    "resolve_compile_device",
    "synchronize_device",
    "tensor_difference",
    "torch_compile_graph_break_count",
]
