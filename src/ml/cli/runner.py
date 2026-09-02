"""Small argparse runner with strict JSON output and dependency guidance."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast


def jsonable(value: Any) -> Any:
    """Convert command results to values accepted by strict JSON encoders."""

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return jsonable(to_dict())
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if hasattr(type(value), "__attrs_attrs__"):
        import attrs

        return jsonable(attrs.asdict(value))
    return value


def print_json(value: Any) -> None:
    """Print one deterministic, standards-compliant JSON document."""

    print(json.dumps(jsonable(value), allow_nan=False, indent=2, sort_keys=True))


def _optional_dependency(
    error: BaseException,
    dependency_groups: Mapping[str, str],
) -> tuple[ModuleNotFoundError, str] | None:
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))

        if isinstance(current, ModuleNotFoundError):
            missing = (current.name or "").split(".", maxsplit=1)[0]
            dependency_group = dependency_groups.get(missing)
            if dependency_group is not None:
                return current, dependency_group

        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return None


def invoke(
    action: Callable[[], Any],
    *,
    dependency_groups: Mapping[str, str],
) -> int:
    """Invoke a parsed command and map expected operational errors to exit
    2."""

    try:
        result = action()
    except (ModuleNotFoundError, OSError, RuntimeError, ValueError) as error:
        optional_dependency = _optional_dependency(error, dependency_groups)
        if optional_dependency is not None:
            missing_error, dependency_group = optional_dependency
            print(
                f"missing optional ML dependency {missing_error.name!r}; "
                f"run `uv sync --locked --group {dependency_group}` or "
                "`make setup-ml`",
                file=sys.stderr,
            )
            return 2
        if isinstance(error, ModuleNotFoundError):
            raise
        print(f"error: {error}", file=sys.stderr)
        return 2

    if result is not None:
        print_json(result)
    return 0


def run_parser(
    parser: argparse.ArgumentParser,
    argv: Sequence[str] | None = None,
    *,
    dependency_groups: Mapping[str, str],
) -> int:
    """Parse ``argv`` and invoke the handler stored by ``set_defaults``."""

    arguments = parser.parse_args(argv)
    handler = cast(Callable[[argparse.Namespace], Any], arguments.handler)
    return invoke(
        lambda: handler(arguments),
        dependency_groups=dependency_groups,
    )
