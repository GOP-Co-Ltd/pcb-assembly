"""再現可能なPyTorch学習の乱数状態管理."""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import cast

import numpy as np
import torch
from torch import Tensor


def seed_everything(seed: int, *, deterministic: bool) -> None:
    """Python、NumPy、PyTorchへ同じrun seedを設定する."""

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(deterministic)


def capture_random_state() -> dict[str, object]:
    """checkpointへ保存可能な全乱数generator状態を返す."""

    numpy_state = cast(tuple[str, np.ndarray, int, int, float], np.random.get_state())
    return {
        "python": random.getstate(),
        "numpy_bit_generator": numpy_state[0],
        "numpy_state": torch.from_numpy(numpy_state[1].copy()),
        "numpy_position": int(numpy_state[2]),
        "numpy_has_gaussian": bool(numpy_state[3]),
        "numpy_cached_gaussian": float(numpy_state[4]),
        "torch_cpu": torch.get_rng_state(),
        "torch_cuda": (
            torch.cuda.get_rng_state_all() if torch.cuda.is_available() else []
        ),
    }


def restore_random_state(state: Mapping[str, object]) -> None:
    """``capture_random_state``が返した乱数状態を復元する."""

    random.setstate(cast(tuple[object, ...], state["python"]))
    numpy_tensor = cast(Tensor, state["numpy_state"])
    np.random.set_state(
        (
            cast(str, state["numpy_bit_generator"]),
            numpy_tensor.cpu().numpy().astype(np.uint32, copy=False),
            cast(int, state["numpy_position"]),
            cast(bool, state["numpy_has_gaussian"]),
            cast(float, state["numpy_cached_gaussian"]),
        )
    )
    torch.set_rng_state(cast(Tensor, state["torch_cpu"]))
    cuda_states = cast(list[Tensor], state["torch_cuda"])
    if cuda_states:
        if not torch.cuda.is_available():
            raise ValueError("checkpoint contains CUDA RNG state on a CPU-only host")
        torch.cuda.set_rng_state_all(cuda_states)


__all__ = ["capture_random_state", "restore_random_state", "seed_everything"]
