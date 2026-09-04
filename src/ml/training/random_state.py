"""乱数状態の seed・捕捉・復元.

学習を中断点から再開したとき、続きの乱数列が中断しなかった場合と一致するように
Python・NumPy・PyTorch の generator 状態をまとめて checkpoint へ載せる。

NumPy の key 配列は ``torch.load(weights_only=True)`` が ``np.ndarray`` を
復元できないため Tensor へ変換して持つ。

weights_only load は許可した型しか組み立てないので、``np.ndarray`` は
そのままでは往復できない。
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any, cast

import attrs
import numpy as np
import torch
from torch import Tensor

_RANDOM_STATE_KEYS = frozenset(
    {
        "python_state",
        "numpy_bit_generator",
        "numpy_keys",
        "numpy_position",
        "numpy_has_gaussian",
        "numpy_cached_gaussian",
        "torch_cpu_state",
        "torch_cuda_states",
    }
)


def seed_everything(seed: int, *, deterministic: bool) -> None:
    """Python・NumPy・PyTorch へ同じ run seed を設定する.

    ``deterministic`` は PyTorch の決定的アルゴリズム選択に渡す。
    """

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(deterministic)


@attrs.frozen(eq=False)
class RandomState:
    """Checkpoint へ載せられる形にした全 generator の状態.

    Tensor は要素ごとの比較になり真偽値へ落ちないので、等価性は identity で決める。
    """

    python_state: tuple[object, ...]
    numpy_bit_generator: str
    numpy_keys: Tensor
    numpy_position: int
    numpy_has_gaussian: bool
    numpy_cached_gaussian: float
    torch_cpu_state: Tensor
    torch_cuda_states: tuple[Tensor, ...]

    @classmethod
    def capture(cls) -> RandomState:
        """現在の generator 状態を写し取る."""

        numpy_state = cast(
            tuple[str, np.ndarray, int, int, float], np.random.get_state()
        )
        return cls(
            python_state=random.getstate(),
            numpy_bit_generator=numpy_state[0],
            numpy_keys=torch.from_numpy(numpy_state[1].copy()),
            numpy_position=int(numpy_state[2]),
            numpy_has_gaussian=bool(numpy_state[3]),
            numpy_cached_gaussian=float(numpy_state[4]),
            torch_cpu_state=torch.get_rng_state(),
            torch_cuda_states=(
                tuple(torch.cuda.get_rng_state_all())
                if torch.cuda.is_available()
                else ()
            ),
        )

    def restore(self) -> tuple[bool, str | None]:
        """捕捉した状態へ generator を戻す.

        CUDA を持たない host へ CUDA の状態を戻せない場合は、何も変更せず理由を返す。
        """

        if self.torch_cuda_states and not torch.cuda.is_available():
            return False, (
                "CUDA の乱数状態を CPU 専用 host へ復元できません: "
                f"{len(self.torch_cuda_states)} device 分の状態があります"
            )
        random.setstate(cast(Any, self.python_state))
        np.random.set_state(
            cast(
                Any,
                (
                    self.numpy_bit_generator,
                    self.numpy_keys.cpu().numpy().astype(np.uint32, copy=False),
                    self.numpy_position,
                    self.numpy_has_gaussian,
                    self.numpy_cached_gaussian,
                ),
            )
        )
        torch.set_rng_state(self.torch_cpu_state)
        if self.torch_cuda_states:
            torch.cuda.set_rng_state_all(list(self.torch_cuda_states))
        return True, None

    def to_payload(self) -> dict[str, object]:
        """``torch.save`` へ渡せる素の値だけの写像を返す."""

        return {
            "python_state": self.python_state,
            "numpy_bit_generator": self.numpy_bit_generator,
            "numpy_keys": self.numpy_keys,
            "numpy_position": self.numpy_position,
            "numpy_has_gaussian": self.numpy_has_gaussian,
            "numpy_cached_gaussian": self.numpy_cached_gaussian,
            "torch_cpu_state": self.torch_cpu_state,
            "torch_cuda_states": self.torch_cuda_states,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> tuple[RandomState | None, str | None]:
        """:meth:`to_payload` が作った写像から復元する.

        キー集合が完全一致しない場合は理由文字列を返す。
        """

        if error := _key_set_error(payload, _RANDOM_STATE_KEYS, "乱数状態"):
            return None, error
        return (
            cls(
                python_state=tuple(cast(tuple[object, ...], payload["python_state"])),
                numpy_bit_generator=str(payload["numpy_bit_generator"]),
                numpy_keys=cast(Tensor, payload["numpy_keys"]),
                numpy_position=int(cast(int, payload["numpy_position"])),
                numpy_has_gaussian=bool(payload["numpy_has_gaussian"]),
                numpy_cached_gaussian=float(
                    cast(float, payload["numpy_cached_gaussian"])
                ),
                torch_cpu_state=cast(Tensor, payload["torch_cpu_state"]),
                torch_cuda_states=tuple(
                    cast(tuple[Tensor, ...], payload["torch_cuda_states"])
                ),
            ),
            None,
        )


def _key_set_error(
    payload: Mapping[str, object], expected: frozenset[str], description: str
) -> str | None:
    """Payload のキー集合が期待と完全一致しているかを検証する."""

    keys = set(payload)
    if missing := sorted(expected - keys):
        return f"{description} payload に必要なキーがありません: {missing}"
    if unknown := sorted(keys - expected):
        return f"{description} payload に未知のキーがあります: {unknown}"
    return None


__all__ = [
    "RandomState",
    "seed_everything",
]
