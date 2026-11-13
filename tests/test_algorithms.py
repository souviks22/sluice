"""
Unit + integration tests for all three rate limiter algorithms.
Uses fakeredis — no live Redis required.
"""

from __future__ import annotations

import fakeredis
import pytest
import pytest_asyncio
import sys
import os
import math
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sluice.backends import RedisBackend
from sluice.algorithms import TokenBucket, SlidingWindowLog, SlidingWindowCounter


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def backend():
    redis_client = fakeredis.FakeAsyncRedis()
    b = RedisBackend(client=redis_client)
    await b.connect()
    yield b
    await b.close()

# ── Token Bucket ──────────────────────────────────────────────────────────

class TestTokenBucket:

    @pytest.mark.asyncio
    async def test_allows_within_capacity(self, backend):
        limiter = TokenBucket(backend, capacity=10, refill_rate=1.0)
        for _ in range(10):
            r = await limiter.check("u1")
            assert r.allowed, "should allow up to capacity"

    @pytest.mark.asyncio
    async def test_rejects_beyond_capacity(self, backend):
        limiter = TokenBucket(backend, capacity=5, refill_rate=1.0)
        for _ in range(5):
            await limiter.check("u1")
        r = await limiter.check("u1")
        assert not r.allowed, "should reject when tokens exhausted"

    @pytest.mark.asyncio
    async def test_refills_over_time(self, backend):
        limiter = TokenBucket(backend, capacity=5, refill_rate=2.0)  # 2 tokens/sec
        for _ in range(5):
            await limiter.check("u1")
        # Redis TIME is authoritative; wait for one token to refill.
        await asyncio.sleep(0.6)
        r = await limiter.check("u1")
        assert r.allowed, "should allow after refill"
        assert r.remaining <= 5

    @pytest.mark.asyncio
    async def test_admission_does_not_use_application_clock(self, backend, monkeypatch):
        async def forbidden_clock():
            raise AssertionError("Redis admission must use Redis TIME inside Lua")

        monkeypatch.setattr(backend, "now_ms", forbidden_clock)
        limiter = TokenBucket(backend, capacity=2, refill_rate=1.0)
        result = await limiter.check("server-clock")
        assert result.allowed

    @pytest.mark.asyncio
    async def test_weighted_cost_is_all_or_nothing(self, backend):
        limiter = TokenBucket(backend, capacity=5, refill_rate=1.0)
        first = await limiter.check("weighted", cost=3)
        assert first.allowed and first.remaining == 2
        second = await limiter.check("weighted", cost=3)
        assert not second.allowed
        final = await limiter.check("weighted", cost=2)
        assert final.allowed and final.remaining == 0

    @pytest.mark.asyncio
    async def test_retry_after_nonzero_when_rejected(self, backend):
        limiter = TokenBucket(backend, capacity=1, refill_rate=1.0)
        await limiter.check("u1")
        r = await limiter.check("u1")
        assert not r.allowed
        assert r.retry_after_ms > 0

    @pytest.mark.asyncio
    async def test_key_isolation(self, backend):
        limiter = TokenBucket(backend, capacity=2, refill_rate=1.0)
        await limiter.check("u1")
        await limiter.check("u1")
        # u2 should still have full capacity
        r = await limiter.check("u2")
        assert r.allowed
        assert r.remaining == 1  # capacity=2, consumed 1

    @pytest.mark.asyncio
    async def test_algorithm_label(self, backend):
        limiter = TokenBucket(backend, capacity=10, refill_rate=1.0)
        r = await limiter.check("u1")
        assert r.algorithm == "token_bucket"


# ── Sliding Window Log ────────────────────────────────────────────────────

class TestSlidingWindowLog:

    @pytest.mark.asyncio
    async def test_allows_within_limit(self, backend):
        limiter = SlidingWindowLog(backend, limit=5, window_ms=10_000)
        for _ in range(5):
            r = await limiter.check("u1")
            assert r.allowed

    @pytest.mark.asyncio
    async def test_rejects_over_limit(self, backend):
        limiter = SlidingWindowLog(backend, limit=3, window_ms=10_000)
        for _ in range(3):
            await limiter.check("u1")
        r = await limiter.check("u1")
        assert not r.allowed

    @pytest.mark.asyncio
    async def test_allows_after_window_expires(self, backend):
        limiter = SlidingWindowLog(backend, limit=3, window_ms=50)
        for _ in range(3):
            await limiter.check("u1")
        # Wait beyond the configured window (Redis TIME is authoritative).
        await asyncio.sleep(0.06)
        r = await limiter.check("u1")
        assert r.allowed, "window should have expired"

    @pytest.mark.asyncio
    async def test_weighted_cost_is_all_or_nothing(self, backend):
        limiter = SlidingWindowLog(backend, limit=5, window_ms=10_000)
        first = await limiter.check("weighted", cost=3)
        assert first.allowed and first.remaining == 2
        second = await limiter.check("weighted", cost=3)
        assert not second.allowed
        final = await limiter.check("weighted", cost=2)
        assert final.allowed and final.remaining == 0

    @pytest.mark.asyncio
    async def test_weighted_retry_waits_for_enough_entries_to_expire(self, backend):
        limiter = SlidingWindowLog(backend, limit=5, window_ms=1000)
        key = "rl:swl:weighted-retry"
        now_ms = await backend.now_ms()
        await backend._client.zadd(
            key,
            {f"prior-{offset}": now_ms - 500 + offset * 100 for offset in range(5)},
        )

        rejected = await limiter.check("weighted-retry", cost=3)
        assert not rejected.allowed
        # The first three of five prior units must expire before cost=3 fits.
        assert 650 <= rejected.retry_after_ms <= 700

    @pytest.mark.asyncio
    async def test_algorithm_label(self, backend):
        limiter = SlidingWindowLog(backend, limit=10, window_ms=10_000)
        r = await limiter.check("u1")
        assert r.algorithm == "sliding_window_log"


# ── Sliding Window Counter ────────────────────────────────────────────────

class TestSlidingWindowCounter:

    @pytest.mark.asyncio
    async def test_allows_within_limit(self, backend):
        limiter = SlidingWindowCounter(backend, limit=10, window_ms=10_000)
        for _ in range(10):
            r = await limiter.check("u1")
            assert r.allowed

    @pytest.mark.asyncio
    async def test_rejects_over_limit(self, backend):
        limiter = SlidingWindowCounter(backend, limit=5, window_ms=10_000)
        for _ in range(5):
            await limiter.check("u1")
        r = await limiter.check("u1")
        assert not r.allowed

    @pytest.mark.asyncio
    async def test_weighted_cost_is_all_or_nothing(self, backend):
        limiter = SlidingWindowCounter(backend, limit=5, window_ms=10_000)
        first = await limiter.check("weighted", cost=3)
        assert first.allowed and first.remaining == 2
        second = await limiter.check("weighted", cost=3)
        assert not second.allowed
        final = await limiter.check("weighted", cost=2)
        assert final.allowed and final.remaining == 0

    @pytest.mark.asyncio
    async def test_algorithm_label(self, backend):
        limiter = SlidingWindowCounter(backend, limit=10, window_ms=10_000)
        r = await limiter.check("u1")
        assert r.algorithm == "sliding_window_counter"



# ── Cross-algorithm fairness ───────────────────────────────────────────────

class TestCrossAlgorithm:

    @pytest.mark.asyncio
    async def test_all_reject_sustained_overload(self, backend):
        """All three algorithms must reject sustained traffic above limit."""
        limit     = 5
        window_ms = 10_000

        for algo_cls, kwargs in [
            (TokenBucket, {"capacity": limit, "refill_rate": limit / (window_ms / 1000)}),
            (SlidingWindowLog, {"limit": limit, "window_ms": window_ms}),
            (SlidingWindowCounter, {"limit": limit, "window_ms": window_ms}),
        ]:
            limiter = algo_cls(backend, **kwargs)
            allowed = 0
            # Send 50 requests all within 100ms — no meaningful refill period
            for i in range(50):
                r = await limiter.check("u1")
                if r.allowed:
                    allowed += 1

            # For token bucket: capacity=5, refill over 98ms = 5 * 0.098 = 0.49 extra → floor = 0
            # For window algorithms: exactly limit
            if algo_cls == TokenBucket:
                refill_during_run = kwargs["refill_rate"] * (50 * 2 / 1000)
                max_allowed = limit + math.ceil(refill_during_run) + 1
            else:
                max_allowed = limit + 1

            assert allowed <= max_allowed, (
                f"{algo_cls.__name__} admitted {allowed} > {max_allowed} "
                f"(limit={limit}, refill scenario)"
            )
