"""Deterministic, in-process benchmarks for Sluice algorithms.

The harness measures Python/FallbackBackend execution cost, not Redis network
latency. Synthetic request timestamps are supplied through a test clock so
traffic-shape comparisons do not depend on wall-clock sleeps.
"""
from __future__ import annotations

import platform
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from sluice.algorithms import SlidingWindowCounter, SlidingWindowLog, TokenBucket
from sluice.backends import FallbackBackend
from benchmarks.traffic_patterns import Request


class _SimulatedClockBackend(FallbackBackend):
    """Fallback backend whose operation clock is advanced by the workload."""

    def __init__(self) -> None:
        super().__init__()
        self.current_ms = 0

    async def now_ms(self) -> int:
        return self.current_ms


@dataclass
class BenchmarkResult:
    algorithm: str
    pattern_name: str
    total_requests: int
    allowed: int
    rejected: int
    memory_bytes: int
    latency_p50_us: float
    latency_p95_us: float
    latency_p99_us: float
    latency_mean_us: float
    elapsed_seconds: float
    throughput_ops_sec: float
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def allow_rate(self) -> float:
        return self.allowed / max(1, self.total_requests)

    @property
    def reject_rate(self) -> float:
        return self.rejected / max(1, self.total_requests)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["allow_rate"] = self.allow_rate
        result["reject_rate"] = self.reject_rate
        return result


def percentile(values: list[float], quantile: float) -> float:
    """Nearest-rank percentile, with explicit empty-input behavior."""
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int((quantile * len(ordered) + 0.999999999)))
    return ordered[min(rank - 1, len(ordered) - 1)]


async def run_single(
    algorithm_name: str,
    pattern_name: str,
    requests: list[Request],
    limit: int = 100,
    window_ms: int = 60_000,
    capacity: int = 100,
    refill_rate: float = 1.667,
) -> BenchmarkResult:
    """Run one algorithm/workload pair against isolated local state."""
    backend = _SimulatedClockBackend()
    if algorithm_name == "token_bucket":
        limiter = TokenBucket(backend, capacity=capacity, refill_rate=refill_rate)
    elif algorithm_name == "sliding_window_log":
        limiter = SlidingWindowLog(backend, limit=limit, window_ms=window_ms)
    elif algorithm_name == "sliding_window_counter":
        limiter = SlidingWindowCounter(backend, limit=limit, window_ms=window_ms)
    else:
        raise ValueError(f"Unknown algorithm: {algorithm_name}")

    allowed = rejected = 0
    latencies: list[float] = []
    started = time.perf_counter()
    for request in requests:
        backend.current_ms = int(request.timestamp_ms)
        before = time.perf_counter_ns()
        result = await limiter.check(request.identifier)
        latencies.append((time.perf_counter_ns() - before) / 1_000)
        if result.allowed:
            allowed += 1
        else:
            rejected += 1
    elapsed = max(time.perf_counter() - started, 1e-12)

    return BenchmarkResult(
        algorithm=algorithm_name,
        pattern_name=pattern_name,
        total_requests=len(requests),
        allowed=allowed,
        rejected=rejected,
        memory_bytes=backend.estimate_memory_bytes(),
        latency_p50_us=percentile(latencies, 0.50),
        latency_p95_us=percentile(latencies, 0.95),
        latency_p99_us=percentile(latencies, 0.99),
        latency_mean_us=statistics.fmean(latencies) if latencies else 0.0,
        elapsed_seconds=elapsed,
        throughput_ops_sec=len(requests) / elapsed,
        meta={
            "clock": "simulated",
            "backend": "FallbackBackend",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
    )


ALGORITHMS = ["token_bucket", "sliding_window_log", "sliding_window_counter"]
