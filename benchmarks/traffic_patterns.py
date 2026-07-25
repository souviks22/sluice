"""
Synthetic traffic pattern generators.

Each pattern yields (timestamp_ms, identifier) tuples representing
request arrival events. These are deterministic (seeded) for reproducibility.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class Request:
    timestamp_ms: float
    identifier: str


def uniform(
    rate_per_sec: float,
    duration_sec: float,
    identifier: str = "user:1",
) -> list[Request]:
    """
    Perfectly uniform inter-arrival times (1/rate_per_sec apart).
    Baseline traffic — every rate limiter should pass ~rate*duration requests.
    """
    interval_ms = 1000.0 / rate_per_sec
    t = 0.0
    requests: list[Request] = []
    while t < duration_sec * 1000:
        requests.append(Request(t, identifier))
        t += interval_ms
    return requests


def burst_then_idle(
    burst_size: int,
    burst_at_ms: float,
    idle_rate_per_sec: float,
    duration_sec: float,
    identifier: str = "user:1",
) -> list[Request]:
    """
    `burst_size` requests arrive at once at `burst_at_ms`, then the client
    trickles at `idle_rate_per_sec`.
    Tests burst tolerance differences between token bucket and window limiters.
    """
    requests = [Request(burst_at_ms + i * 0.1, identifier) for i in range(burst_size)]
    t = 0.0
    interval_ms = 1000.0 / idle_rate_per_sec if idle_rate_per_sec > 0 else float("inf")
    while t < duration_sec * 1000:
        if abs(t - burst_at_ms) > 1:  # skip near-burst time
            requests.append(Request(t, identifier))
        t += interval_ms
    requests.sort(key=lambda r: r.timestamp_ms)
    return requests


def window_boundary_hammer(
    limit: int,
    window_ms: int,
    duration_sec: float,
    identifier: str = "user:1",
) -> list[Request]:
    """
    Sends `limit` requests just before each window boundary and `limit` requests
    just after. This is the classic fixed-window double-spend attack pattern.
    Reveals differences between fixed-window vs. sliding-window semantics.
    """
    requests: list[Request] = []
    num_windows = int(duration_sec * 1000 / window_ms)
    for w in range(num_windows):
        boundary_ms = w * window_ms
        # `limit` requests in last 50ms of window
        for i in range(limit):
            t = boundary_ms + window_ms - 50 + (i * 50.0 / limit)
            requests.append(Request(t, identifier))
        # `limit` requests in first 50ms of next window
        for i in range(limit):
            t = boundary_ms + window_ms + (i * 50.0 / limit)
            requests.append(Request(t, identifier))
    requests.sort(key=lambda r: r.timestamp_ms)
    return requests


def poisson(
    avg_rate_per_sec: float,
    duration_sec: float,
    identifier: str = "user:1",
    seed: int = 42,
) -> list[Request]:
    """
    Poisson-distributed inter-arrival times — realistic web traffic model.
    Mean rate = avg_rate_per_sec; variance = same (memoryless property).
    """
    rng = random.Random(seed)
    requests = []
    t = 0.0
    while t < duration_sec * 1000:
        # Exponential inter-arrival: -ln(U) / rate
        inter = -math.log(rng.random()) * (1000.0 / avg_rate_per_sec)
        t += inter
        if t < duration_sec * 1000:
            requests.append(Request(t, identifier))
    return requests


def multi_tenant(
    num_tenants: int,
    rate_per_tenant_per_sec: float,
    duration_sec: float,
    seed: int = 42,
) -> list[Request]:
    """
    Multiple independent tenants each sending at `rate_per_tenant_per_sec`.
    Tests that per-key isolation is working (no cross-tenant interference).
    """
    requests: list[Request] = []
    for tenant_id in range(num_tenants):
        tenant_requests = poisson(
            rate_per_tenant_per_sec,
            duration_sec,
            identifier=f"tenant:{tenant_id}",
            seed=seed + tenant_id,
        )
        requests.extend(tenant_requests)
    requests.sort(key=lambda r: r.timestamp_ms)
    return requests
