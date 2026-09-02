"""Runtime benchmark timing and host-evidence mechanics."""

from __future__ import annotations

import os
import platform
import resource
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import numpy as np

_RuntimeT = TypeVar("_RuntimeT")
_CaseT = TypeVar("_CaseT")
_MODULE_MONOTONIC_ORIGIN_NS = time.monotonic_ns()


@dataclass(frozen=True)
class CaseLatency:
    label: str
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float


@dataclass(frozen=True)
class RuntimeBenchmark:
    cold_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    cases: tuple[CaseLatency, ...]
    cold_start_clock: str
    cold_start_origin: str


@dataclass(frozen=True)
class BenchmarkEnvironment:
    platform_model: str
    os_id: str
    os_release: str
    python_version: str
    cpu_governor: str
    platform_machine: str
    peak_rss_bytes: int


def benchmark_runtime(
    create_runtime: Callable[[], _RuntimeT],
    cases: Sequence[tuple[str, _CaseT]],
    run_case: Callable[[_RuntimeT, _CaseT], None],
    *,
    warmup_iterations: int,
    measured_iterations: int,
) -> RuntimeBenchmark:
    """Measure process-origin cold latency and per-case warmed
    distributions."""

    if not cases or any(not label for label, _ in cases):
        raise ValueError("benchmark cases must be non-empty and have labels")
    if type(warmup_iterations) is not int or warmup_iterations < 0:
        raise ValueError("warmup_iterations must be a non-negative integer")
    if type(measured_iterations) is not int or measured_iterations < 1:
        raise ValueError("measured_iterations must be a positive integer")

    cold_start_ns, cold_start_origin = process_start_monotonic_ns()
    runtime = create_runtime()
    run_case(runtime, cases[0][1])
    cold_latency_ms = (time.monotonic_ns() - cold_start_ns) / 1_000_000.0

    all_latencies: list[float] = []
    results: list[CaseLatency] = []
    for label, case in cases:
        for _ in range(warmup_iterations):
            run_case(runtime, case)
        latencies: list[float] = []
        for _ in range(measured_iterations):
            started = time.perf_counter_ns()
            run_case(runtime, case)
            latencies.append((time.perf_counter_ns() - started) / 1_000_000.0)
        all_latencies.extend(latencies)
        results.append(
            CaseLatency(
                label=label,
                p50_latency_ms=float(np.percentile(latencies, 50)),
                p95_latency_ms=float(np.percentile(latencies, 95)),
                p99_latency_ms=float(np.percentile(latencies, 99)),
            )
        )

    return RuntimeBenchmark(
        cold_latency_ms=cold_latency_ms,
        p50_latency_ms=float(np.percentile(all_latencies, 50)),
        p95_latency_ms=max(item.p95_latency_ms for item in results),
        p99_latency_ms=max(item.p99_latency_ms for item in results),
        cases=tuple(results),
        cold_start_clock="CLOCK_MONOTONIC",
        cold_start_origin=cold_start_origin,
    )


def platform_model() -> str:
    path = Path("/proc/device-tree/model")
    if path.is_file():
        try:
            return path.read_bytes().rstrip(b"\x00").decode("utf-8")
        except (OSError, UnicodeDecodeError):
            pass
    return f"{platform.system()} {platform.machine()}"


def process_start_monotonic_ns() -> tuple[int, str]:
    stat_path = Path("/proc/self/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8")
        fields_after_command = raw[raw.rindex(")") + 2 :].split()
        start_ticks = int(fields_after_command[19])
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        if start_ticks >= 0 and clock_ticks > 0:
            return (
                start_ticks * 1_000_000_000 // clock_ticks,
                "process-start:/proc/self/stat",
            )
    except (OSError, ValueError, IndexError):
        pass
    return _MODULE_MONOTONIC_ORIGIN_NS, "module-import:fallback"


def os_id() -> str:
    path = Path("/etc/os-release")
    try:
        values = {
            key: value.strip().strip('"')
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
            for key, value in (line.split("=", 1),)
        }
    except OSError:
        return platform.system().lower() or "unknown"
    return values.get("ID", platform.system().lower() or "unknown")


def cpu_governor() -> str:
    path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "unavailable"
    return value or "unavailable"


def peak_rss_bytes() -> int:
    maximum_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maximum_rss if platform.system() == "Darwin" else maximum_rss * 1024)


def benchmark_environment() -> BenchmarkEnvironment:
    """Capture the portable host fields persisted alongside timing evidence."""

    return BenchmarkEnvironment(
        platform_model=platform_model(),
        os_id=os_id(),
        os_release=platform.release(),
        python_version=platform.python_version(),
        cpu_governor=cpu_governor(),
        platform_machine=platform.machine(),
        peak_rss_bytes=peak_rss_bytes(),
    )
