"""
Benchmark runner.

Uses FallbackBackend (in-process) so results are reproducible without a live Redis.
The benchmark simulates time by injecting `now_ms` directly into Lua scripts —
no actual sleeping.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import time
from dataclasses import dataclass, field
from typing import Any

from sluice.backends import FallbackBackend
from sluice.algorithms import TokenBucket, SlidingWindowLog, SlidingWindowCounter
from benchmarks.traffic_patterns import Request


@dataclass
class BenchmarkResult:
    algorithm: str
    pattern_name: str
    total_requests: int
    allowed: int
    rejected: int
    # Memory sampled from Redis after the run
    memory_bytes: int
    # Latency stats (wall-clock microseconds per check() call)
    latency_p50_us: float
    latency_p99_us: float
    latency_mean_us: float
    # Extra metadata
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def allow_rate(self) -> float:
        return self.allowed / max(1, self.total_requests)

    @property
    def reject_rate(self) -> float:
        return self.rejected / max(1, self.total_requests)


async def run_single(
    algorithm_name: str,
    pattern_name: str,
    requests: list[Request],
    limit: int = 100,
    window_ms: int = 60_000,
    capacity: int = 100,
    refill_rate: float = 1.667,  # 100/60
) -> BenchmarkResult:
    """Run one (algorithm × traffic pattern) combination."""
    backend = FallbackBackend()

    if algorithm_name == "token_bucket":
        limiter = TokenBucket(backend, capacity=capacity, refill_rate=refill_rate)
    elif algorithm_name == "sliding_window_log":
        limiter = SlidingWindowLog(backend, limit=limit, window_ms=window_ms)
    elif algorithm_name == "sliding_window_counter":
        limiter = SlidingWindowCounter(backend, limit=limit, window_ms=window_ms)
    else:
        raise ValueError(f"Unknown algorithm: {algorithm_name}")

    allowed_count = 0
    rejected_count = 0
    latencies: list[float] = []

    for req in requests:
        # Inject simulated time into backend so algorithms use our synthetic clock
        async def _now_ms():
            return int(req.timestamp_ms)
        backend.now_ms = _now_ms

        t0 = time.perf_counter()
        result = await limiter.check(req.identifier)
        elapsed_us = (time.perf_counter() - t0) * 1_000_000

        latencies.append(elapsed_us)
        if result.allowed:
            allowed_count += 1
        else:
            rejected_count += 1

    memory_bytes = backend.estimate_memory_bytes()

    latencies.sort()
    n = len(latencies)
    p50 = latencies[int(n * 0.50)] if n else 0
    p99 = latencies[int(n * 0.99)] if n else 0
    mean = sum(latencies) / n if n else 0

    return BenchmarkResult(
        algorithm=algorithm_name,
        pattern_name=pattern_name,
        total_requests=len(requests),
        allowed=allowed_count,
        rejected=rejected_count,
        memory_bytes=memory_bytes,
        latency_p50_us=p50,
        latency_p99_us=p99,
        latency_mean_us=mean,
    )

ALGORITHMS = ["token_bucket", "sliding_window_log", "sliding_window_counter"]
