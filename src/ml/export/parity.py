"""Model-agnostic scalar eager/runtime parity calculations."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ScalarParity:
    reference: float
    actual: float
    absolute_error: float
    tolerance: float
    passed: bool


def compare_eager_outputs(
    eager_outputs: Sequence[float],
    runtime_outputs: Sequence[float],
    *,
    absolute_tolerances: Sequence[float],
    relative_tolerances: Sequence[float],
) -> tuple[ScalarParity, ...]:
    """Compare corresponding scalar outputs using ``max(atol, |ref|*rtol)``."""

    lengths = {
        len(eager_outputs),
        len(runtime_outputs),
        len(absolute_tolerances),
        len(relative_tolerances),
    }
    if len(lengths) != 1 or not eager_outputs:
        raise ValueError("parity output and tolerance sequences must have equal length")
    results: list[ScalarParity] = []
    for reference, actual, absolute, relative in zip(
        eager_outputs,
        runtime_outputs,
        absolute_tolerances,
        relative_tolerances,
        strict=True,
    ):
        if (
            not math.isfinite(absolute)
            or not math.isfinite(relative)
            or absolute < 0
            or relative < 0
        ):
            raise ValueError("parity tolerances must be finite and non-negative")
        finite = math.isfinite(reference) and math.isfinite(actual)
        error = abs(actual - reference) if finite else math.inf
        tolerance = max(absolute, abs(reference) * relative) if finite else absolute
        results.append(
            ScalarParity(
                reference=reference,
                actual=actual,
                absolute_error=error,
                tolerance=tolerance,
                passed=finite and error <= tolerance,
            )
        )
    return tuple(results)
