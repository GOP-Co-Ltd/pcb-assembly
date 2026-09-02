"""PyTorch modelの静的な規模情報."""

from torch import nn


def model_parameter_count(model: nn.Module) -> int:
    """モデルの全parameter数を返す."""

    return sum(parameter.numel() for parameter in model.parameters())


__all__ = ["model_parameter_count"]
