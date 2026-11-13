"""Weighted admission and bounded-state guarantees for the local backend."""
from __future__ import annotations

import pytest

from sluice.backends import FallbackBackend
from sluice.domain import RateLimitOperation, SlidingWindowConfig, TokenBucketConfig


def operation(algorithm: str, config, cost: int = 1, key: str = "rl:test"):
    return RateLimitOperation(algorithm=algorithm, key=key, config=config, cost=cost)


@pytest.mark.asyncio
async def test_weighted_log_rejection_does_not_partially_consume_quota():
    backend = FallbackBackend()
    async def clock(): return 1_000
    backend.now_ms = clock
    config = SlidingWindowConfig(limit=5, window_ms=10_000)
    first = await backend.check(operation("sliding_window_log", config, cost=3))
    rejected = await backend.check(operation("sliding_window_log", config, cost=3))
    assert first.allowed and first.remaining == 2
    assert not rejected.allowed and rejected.remaining == 2
    assert len(backend._sliding_log_cache["rl:test"]) == 3


@pytest.mark.asyncio
async def test_weighted_counter_rejection_does_not_partially_consume_quota():
    backend = FallbackBackend()
    async def clock(): return 1_000
    backend.now_ms = clock
    config = SlidingWindowConfig(limit=5, window_ms=10_000)
    first = await backend.check(operation("sliding_window_counter", config, cost=3))
    rejected = await backend.check(operation("sliding_window_counter", config, cost=3))
    assert first.allowed and first.remaining == 2
    assert not rejected.allowed and rejected.remaining == 2
    assert backend._sliding_counter_cache["rl:test"]["current"] == 3


@pytest.mark.asyncio
async def test_weighted_token_bucket_is_all_or_nothing():
    backend = FallbackBackend()
    async def clock(): return 1_000
    backend.now_ms = clock
    config = TokenBucketConfig(capacity=5, refill_rate=1)
    first = await backend.check(operation("token_bucket", config, cost=3))
    rejected = await backend.check(operation("token_bucket", config, cost=3))
    assert first.allowed and first.remaining == 2
    assert not rejected.allowed and rejected.remaining == 2
    assert backend._token_bucket_cache["rl:test"]["tokens"] == 2


def test_fallback_cache_bounds_are_validated():
    with pytest.raises(Exception):
        FallbackBackend(maxsize=0)
    with pytest.raises(Exception):
        FallbackBackend(ttl=float("inf"))

@pytest.mark.asyncio
async def test_concurrent_weighted_log_admissions_never_overconsume():
    import asyncio

    backend = FallbackBackend()
    async def clock(): return 5_000
    backend.now_ms = clock
    config = SlidingWindowConfig(limit=5, window_ms=10_000)
    results = await asyncio.gather(*[
        backend.check(operation("sliding_window_log", config, cost=2))
        for _ in range(8)
    ])
    assert sum(result.allowed for result in results) == 2
    assert len(backend._sliding_log_cache["rl:test"]) == 4


def test_lru_capacity_is_bounded_per_algorithm():
    backend = FallbackBackend(maxsize=1, ttl=30)
    backend._token_bucket_cache["rl:first"] = {"tokens": 1.0, "last_refill_ms": 0.0}
    backend._token_bucket_cache["rl:second"] = {"tokens": 1.0, "last_refill_ms": 0.0}
    assert len(backend._token_bucket_cache) == 1
    assert backend.eviction_counts()["token_bucket"] == 1
