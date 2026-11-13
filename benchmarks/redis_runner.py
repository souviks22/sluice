"""Concurrent benchmark runner for a configured real Redis deployment."""
from __future__ import annotations

import asyncio
import platform
import statistics
import sys
import time
import uuid
from typing import Any

from sluice.algorithms import SlidingWindowCounter, SlidingWindowLog, TokenBucket
from sluice.backends import RedisBackend

from benchmarks.runner import percentile


ALGORITHMS = ("token_bucket", "sliding_window_log", "sliding_window_counter")


async def _run_one(
    *,
    redis_url: str,
    algorithm: str,
    clients: int,
    requests_per_client: int,
    cardinality: int,
    limit: int,
    window_ms: int,
    cost: int,
    topology: str,
) -> dict[str, Any]:
    if algorithm not in ALGORITHMS:
        raise ValueError(f"Unknown algorithm: {algorithm}")
    if min(clients, requests_per_client, cardinality, limit, window_ms, cost) <= 0:
        raise ValueError("benchmark counts, limits, window, and cost must be positive")
    if cost > limit:
        raise ValueError("cost cannot exceed limit/capacity")

    run_id = uuid.uuid4().hex
    prefix = f"sluice:benchmark:{run_id}"
    backends = [RedisBackend.from_url(redis_url, max_connections=2) for _ in range(clients)]
    redis_client = backends[0]._client
    try:
        await asyncio.gather(*(backend.connect() for backend in backends))
        if not all(await asyncio.gather(*(backend.ping() for backend in backends))):
            raise ConnectionError("One or more Redis benchmark clients failed ping()")

        refill_rate = limit / (window_ms / 1000)

        def make_limiter(backend):
            if algorithm == "token_bucket":
                return TokenBucket(
                    backend, capacity=limit, refill_rate=refill_rate, key_prefix=prefix
                )
            if algorithm == "sliding_window_log":
                return SlidingWindowLog(
                    backend, limit=limit, window_ms=window_ms, key_prefix=prefix
                )
            return SlidingWindowCounter(
                backend, limit=limit, window_ms=window_ms, key_prefix=prefix
            )

        limiters = [make_limiter(backend) for backend in backends]
        latencies: list[float] = []
        latency_lock = asyncio.Lock()
        allowed = rejected = 0
        results_lock = asyncio.Lock()

        async def worker(worker_index: int) -> None:
            nonlocal allowed, rejected
            limiter = limiters[worker_index]
            worker_latencies: list[float] = []
            worker_allowed = worker_rejected = 0
            for request_index in range(requests_per_client):
                identifier_number = (
                    worker_index * requests_per_client + request_index
                ) % cardinality
                started_ns = time.perf_counter_ns()
                result = await limiter.check(f"client-{identifier_number}", cost=cost)
                worker_latencies.append((time.perf_counter_ns() - started_ns) / 1_000)
                if result.allowed:
                    worker_allowed += 1
                else:
                    worker_rejected += 1
            async with latency_lock:
                latencies.extend(worker_latencies)
            async with results_lock:
                allowed += worker_allowed
                rejected += worker_rejected

        started = time.perf_counter()
        await asyncio.gather(*(worker(index) for index in range(clients)))
        elapsed = max(time.perf_counter() - started, 1e-12)

        keys = [key async for key in redis_client.scan_iter(match=f"{prefix}:*")]
        key_bytes = 0
        for key in keys:
            key_bytes += await redis_client.memory_usage(key) or 0

        info = await redis_client.info("server")
        total_requests = clients * requests_per_client
        report = {
            "algorithm": algorithm,
            "redis_version": info.get("redis_version", "unknown"),
            "topology": topology,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "clients": clients,
            "requests_per_client": requests_per_client,
            "total_requests": total_requests,
            "cardinality": cardinality,
            "cost": cost,
            "limit": limit,
            "window_ms": window_ms,
            "allowed": allowed,
            "rejected": rejected,
            "elapsed_seconds": elapsed,
            "throughput_ops_sec": total_requests / elapsed,
            "latency_p50_us": percentile(latencies, 0.50),
            "latency_p95_us": percentile(latencies, 0.95),
            "latency_p99_us": percentile(latencies, 0.99),
            "latency_mean_us": statistics.fmean(latencies) if latencies else 0.0,
            "key_count": len(keys),
            "key_memory_bytes": key_bytes,
        }
        return report
    finally:
        try:
            keys = [key async for key in redis_client.scan_iter(match=f"{prefix}:*")]
            if keys:
                await redis_client.delete(*keys)
        except Exception:
            # Preserve the benchmark failure/result if Redis is unavailable
            # during cleanup. Every generated key also has a script TTL.
            pass
        await asyncio.gather(*(backend.close() for backend in backends))


async def run_redis_benchmark(
    *,
    redis_url: str,
    algorithms: tuple[str, ...] = ALGORITHMS,
    clients: int = 8,
    requests_per_client: int = 2_000,
    cardinality: int = 1,
    limit: int = 100,
    window_ms: int = 60_000,
    cost: int = 1,
    topology: str = "unspecified",
) -> list[dict[str, Any]]:
    """Measure Redis-backed admission; all clients share a benchmark namespace."""
    reports = []
    for algorithm in algorithms:
        reports.append(await _run_one(
            redis_url=redis_url,
            algorithm=algorithm,
            clients=clients,
            requests_per_client=requests_per_client,
            cardinality=cardinality,
            limit=limit,
            window_ms=window_ms,
            cost=cost,
            topology=topology,
        ))
    return reports
