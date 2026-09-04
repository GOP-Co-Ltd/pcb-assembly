"""乱数状態の捕捉・復元と、checkpoint 経由の往復の公開契約."""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pytest
import torch

from ml.training.random_state import RandomState, seed_everything


def _draw() -> tuple[float, float, float]:
    """3 系統の乱数から 1 個ずつ引く."""

    return (
        random.random(),
        float(np.random.random()),
        float(torch.rand(1).item()),
    )


class TestSeedEverything:
    """同じ seed なら 3 系統とも同じ並びになる."""

    def test_same_seed_reproduces_the_same_draws(self):
        seed_everything(1234, deterministic=True)
        first = [_draw() for _ in range(3)]

        seed_everything(1234, deterministic=True)
        second = [_draw() for _ in range(3)]

        assert first == second

    def test_different_seed_changes_the_draws(self):
        seed_everything(1234, deterministic=True)
        first = _draw()

        seed_everything(5678, deterministic=True)
        second = _draw()

        assert first != second


class TestRandomStateRoundTrip:
    """捕捉した状態を戻すと、次に出る値が一致する."""

    def test_restore_resumes_the_same_sequence(self):
        seed_everything(7, deterministic=True)
        state = RandomState.capture()
        expected = [_draw() for _ in range(3)]

        restored, reason = state.restore()

        assert reason is None
        assert restored is True
        assert [_draw() for _ in range(3)] == expected

    def test_capture_does_not_consume_randomness(self):
        seed_everything(7, deterministic=True)
        RandomState.capture()
        after_capture = _draw()

        seed_everything(7, deterministic=True)
        without_capture = _draw()

        assert after_capture == without_capture

    def test_payload_round_trip_preserves_the_sequence(self):
        seed_everything(11, deterministic=True)
        state = RandomState.capture()
        expected = [_draw() for _ in range(3)]

        recovered, reason = RandomState.from_payload(state.to_payload())

        assert reason is None
        assert recovered is not None
        assert recovered.restore() == (True, None)
        assert [_draw() for _ in range(3)] == expected

    def test_payload_survives_a_weights_only_torch_load(self, tmp_path: Path):
        # numpy の key 配列は np.ndarray のままだと weights_only load が読めない
        seed_everything(13, deterministic=True)
        state = RandomState.capture()
        expected = [_draw() for _ in range(3)]
        path = tmp_path / "random-state.pt"
        torch.save(state.to_payload(), path)

        payload = torch.load(path, weights_only=True)
        recovered, reason = RandomState.from_payload(payload)

        assert reason is None
        assert recovered is not None
        assert recovered.restore() == (True, None)
        assert [_draw() for _ in range(3)] == expected


class TestRandomStateRejections:
    """壊れた payload と復元できない state は理由つきで拒否する."""

    def test_missing_key_is_rejected(self):
        seed_everything(3, deterministic=True)
        payload = RandomState.capture().to_payload()
        del payload["numpy_position"]

        recovered, reason = RandomState.from_payload(payload)

        assert recovered is None
        assert reason is not None
        assert "numpy_position" in reason

    def test_unknown_key_is_rejected(self):
        seed_everything(3, deterministic=True)
        payload = RandomState.capture().to_payload()
        payload["unexpected"] = 1

        recovered, reason = RandomState.from_payload(payload)

        assert recovered is None
        assert reason is not None
        assert "unexpected" in reason

    def test_cuda_state_cannot_be_restored_on_a_cpu_only_host(self):
        if torch.cuda.is_available():
            pytest.skip("CUDA が使える環境では CPU 専用ホストの拒否を検証できません")
        seed_everything(3, deterministic=True)
        state = RandomState.capture()
        with_cuda = RandomState(
            python_state=state.python_state,
            numpy_bit_generator=state.numpy_bit_generator,
            numpy_keys=state.numpy_keys,
            numpy_position=state.numpy_position,
            numpy_has_gaussian=state.numpy_has_gaussian,
            numpy_cached_gaussian=state.numpy_cached_gaussian,
            torch_cpu_state=state.torch_cpu_state,
            torch_cuda_states=(torch.zeros(8, dtype=torch.uint8),),
        )

        restored, reason = with_cuda.restore()

        assert restored is False
        assert reason is not None
        assert "CUDA" in reason.upper()
