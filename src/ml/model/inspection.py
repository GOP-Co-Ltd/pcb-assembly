"""Model の大きさを実測する.

parameter 数と multiply-accumulate 数は解析的に数えない。

実 module へ forward hook を挿し、1 回走らせて実測する。

config と実装が食い違ったときに、古い数え上げが通るのを防ぐため。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import attrs
import torch
from torch import Tensor, nn
from torch.utils.hooks import RemovableHandle

_MULTIPLY_ACCUMULATE_PER_GIGA = 1e9


def count_parameters(model: nn.Module, *, trainable_only: bool = False) -> int:
    """Model が持つ parameter の要素数を数える."""

    return sum(
        parameter.numel()
        for parameter in model.parameters()
        if parameter.requires_grad or not trainable_only
    )


@attrs.frozen
class ModelSize:
    """1 sample を推論するときの model の大きさ."""

    parameter_count: int
    trainable_parameter_count: int
    multiply_accumulate_count: int

    @property
    def giga_multiply_accumulate(self) -> float:
        """Multiply-accumulate 数を GMAC 単位で返す."""

        return self.multiply_accumulate_count / _MULTIPLY_ACCUMULATE_PER_GIGA


def measure_model_size(model: nn.Module, example_inputs: Sequence[Tensor]) -> ModelSize:
    """1 回 forward して parameter 数と multiply-accumulate 数を実測する.

    数えるのは Conv2d と Linear だけとする。

    GroupNorm・ReLU・pooling の要素演算を含めない GMAC の慣用に合わせるため。
    """

    _reject_non_single_sample_inputs(example_inputs)
    counts: list[int] = []

    def record(module: nn.Module, inputs: Any, output: Any) -> None:
        counts.append(_multiply_accumulate_count(module, output))

    handles: list[RemovableHandle] = [
        module.register_forward_hook(record)
        for module in model.modules()
        if isinstance(module, nn.Conv2d | nn.Linear)
    ]
    was_training = model.training
    try:
        model.eval()
        with torch.no_grad():
            model(*example_inputs)
    finally:
        model.train(was_training)
        for handle in handles:
            handle.remove()
    return ModelSize(
        parameter_count=count_parameters(model),
        trainable_parameter_count=count_parameters(model, trainable_only=True),
        multiply_accumulate_count=sum(counts),
    )


def _multiply_accumulate_count(module: nn.Module, output: Any) -> int:
    if not isinstance(output, Tensor):
        return 0
    if isinstance(module, nn.Conv2d):
        kernel_elements = module.kernel_size[0] * module.kernel_size[1]
        input_channels = module.in_channels // module.groups
        return output.numel() * input_channels * kernel_elements
    if isinstance(module, nn.Linear):
        return output.numel() * module.in_features
    return 0


def _reject_non_single_sample_inputs(example_inputs: Sequence[Tensor]) -> None:
    if not example_inputs:
        raise ValueError("example_inputs は 1 個以上の tensor が必要です")
    for example in example_inputs:
        if example.ndim < 1 or int(example.shape[0]) != 1:
            raise ValueError(
                "example_inputs は batch 次元が 1 の tensor が必要です: "
                f"{tuple(example.shape)}"
            )


__all__ = [
    "ModelSize",
    "count_parameters",
    "measure_model_size",
]
