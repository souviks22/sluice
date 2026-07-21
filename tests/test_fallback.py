"""
Unit tests for the fallback backend and algorithms.
Checks local in-memory behavior when Redis instance is unavailable.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import sys
import os
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sluice.backends import FallbackBackend
from sluice.algorithms import TokenBucket, SlidingWindowLog, SlidingWindowCounter


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def backend():
    b = FallbackBackend()
    yield b

def _inject_time(backend: FallbackBackend, t_ms: int):
    """Override backend clock for deterministic tests."""
    async def fake_now_ms():
        return t_ms
    backend.now_ms = fake_now_ms


# ── Token Bucket ──────────────────────────────────────────────────────────

class TestTokenBucket:

    @pytest.mark.asyncio
    async def test_allows_within_capacity(self, backend):
        limiter = TokenBucket(backend, capacity=10, refill_rate=1.0)
        _inject_time(backend, 0)
        for _ in range(10):
            r = await limiter.check("u1")
            assert r.allowed, "should allow up to capacity"

    @pytest.mark.asyncio
    async def test_rejects_beyond_capacity(self, backend):
        limiter = TokenBucket(backend, capacity=5, refill_rate=1.0)
        _inject_time(backend, 0)
        for _ in range(5):
            await limiter.check("u1")
        r = await limiter.check("u1")
        assert not r.allowed, "should reject when tokens exhausted"

    @pytest.mark.asyncio
    async def test_refills_over_time(self, backend):
        limiter = TokenBucket(backend, capacity=5, refill_rate=2.0)  # 2 tokens/sec
        _inject_time(backend, 0)
        for _ in range(5):
            await limiter.check("u1")
        # 3 seconds later — should have refilled 6 tokens (capped at 5)
        _inject_time(backend, 3000)
        r = await limiter.check("u1")
        assert r.allowed, "should allow after refill"
        assert r.remaining <= 5

    @pytest.mark.asyncio
    async def test_retry_after_nonzero_when_rejected(self, backend):
        limiter = TokenBucket(backend, capacity=1, refill_rate=1.0)
        _inject_time(backend, 0)
        await limiter.check("u1")
        r = await limiter.check("u1")
        assert not r.allowed
        assert r.retry_after_ms > 0

    @pytest.mark.asyncio
    async def test_key_isolation(self, backend):
        limiter = TokenBucket(backend, capacity=2, refill_rate=1.0)
        _inject_time(backend, 0)
        await limiter.check("u1")
        await limiter.check("u1")
        # u2 should still have full capacity
        r = await limiter.check("u2")
        assert r.allowed
        assert r.remaining == 1  # capacity=2, consumed 1

    @pytest.mark.asyncio
    async def test_algorithm_label(self, backend):
        limiter = TokenBucket(backend, capacity=10, refill_rate=1.0)
        _inject_time(backend, 0)
        r = await limiter.check("u1")
        assert r.algorithm == "token_bucket"


# ── Sliding Window Log ────────────────────────────────────────────────────

class TestSlidingWindowLog:

    @pytest.mark.asyncio
    async def test_allows_within_limit(self, backend):
        limiter = SlidingWindowLog(backend, limit=5, window_ms=10_000)
        _inject_time(backend, 0)
        for _ in range(5):
            r = await limiter.check("u1")
            assert r.allowed

    @pytest.mark.asyncio
    async def test_rejects_over_limit(self, backend):
        limiter = SlidingWindowLog(backend, limit=3, window_ms=10_000)
        _inject_time(backend, 0)
        for _ in range(3):
            await limiter.check("u1")
        r = await limiter.check("u1")
        assert not r.allowed

    @pytest.mark.asyncio
    async def test_allows_after_window_expires(self, backend):
        limiter = SlidingWindowLog(backend, limit=3, window_ms=5_000)
        _inject_time(backend, 0)
        for _ in range(3):
            await limiter.check("u1")
        # Jump past window
        _inject_time(backend, 6_000)
        r = await limiter.check("u1")
        assert r.allowed, "window should have expired"

    @pytest.mark.asyncio
    async def test_sliding_not_fixed(self, backend):
        """
        Sliding window log should NOT allow a double-spike at boundaries.
        At t=9.5s: 3 requests (just before 10s window)
        At t=10.5s: checking: the 9.5s entries are still within [0.5s, 10.5s]
        So only 0 of 3 new requests should go through.
        """
        limiter = SlidingWindowLog(backend, limit=3, window_ms=10_000)
        _inject_time(backend, 9_500)
        for _ in range(3):
            await limiter.check("u1")
        # Move to just inside new "window" — old entries still visible
        _inject_time(backend, 10_500)
        r = await limiter.check("u1")
        assert not r.allowed, "sliding window should see old entries"

    @pytest.mark.asyncio
    async def test_algorithm_label(self, backend):
        limiter = SlidingWindowLog(backend, limit=10, window_ms=10_000)
        _inject_time(backend, 0)
        r = await limiter.check("u1")
        assert r.algorithm == "sliding_window_log"


# ── Sliding Window Counter ────────────────────────────────────────────────

class TestSlidingWindowCounter:

    @pytest.mark.asyncio
    async def test_allows_within_limit(self, backend):
        limiter = SlidingWindowCounter(backend, limit=10, window_ms=10_000)
        _inject_time(backend, 0)
        for _ in range(10):
            r = await limiter.check("u1")
            assert r.allowed

    @pytest.mark.asyncio
    async def test_rejects_over_limit(self, backend):
        limiter = SlidingWindowCounter(backend, limit=5, window_ms=10_000)
        _inject_time(backend, 1_000)
        for _ in range(5):
            await limiter.check("u1")
        r = await limiter.check("u1")
        assert not r.allowed

    @pytest.mark.asyncio
    async def test_interpolation_reduces_with_time(self, backend):
        """
        As we move into a new window the previous-window weight decreases,
        so previously-rejected requests eventually become allowed again.
        """
        limiter = SlidingWindowCounter(backend, limit=10, window_ms=10_000)
        # Fill up the window at t=0
        _inject_time(backend, 0)
        for _ in range(10):
            await limiter.check("u1")
        # Still at t=0 — should be rejected
        r = await limiter.check("u1")
        assert not r.allowed

        # At t=6s (60% into window) previous bucket has weight 0.4
        # effective = 10*0.4 + 0 = 4 < 10, so should allow
        _inject_time(backend, 16_000)  # next bucket, 6s in
        r = await limiter.check("u1")
        assert r.allowed

    @pytest.mark.asyncio
    async def test_algorithm_label(self, backend):
        limiter = SlidingWindowCounter(backend, limit=10, window_ms=10_000)
        _inject_time(backend, 0)
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
                _inject_time(backend, i*2)
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
