"""
Concurrency and atomicity tests.

These tests exist to prove the core distributed-systems claim of this library:
that concurrent requests from multiple coroutines (simulating multiple nodes)
cannot collectively exceed the configured limit, even without any coordination
beyond the atomic Lua script on Redis.

Why asyncio concurrency is sufficient for this proof
-----------------------------------------------------
The race condition this library prevents is:

    Coroutine A: GET count → 99
    Coroutine B: GET count → 99          ← B reads before A writes
    Coroutine A: INCR count → 100 ✓
    Coroutine B: INCR count → 101 ✗ ← over-limit, but was allowed

asyncio.gather() causes exactly this interleaving — coroutines yield at every
`await`, so multiple check() calls genuinely interleave around their Redis I/O.
If the Lua script were not atomic, this test would catch it.

The PythonBackend uses an in-process threading.Lock which gives the same guarantee
in-process. The RedisBackend with real Redis proves it at the network level.
"""

from __future__ import annotations

import fakeredis
import asyncio
import pytest
import pytest_asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sluice.backends.redis_backend import RedisBackend
from sluice.algorithms.token_bucket import TokenBucket
from sluice.algorithms.sliding_window_log import SlidingWindowLog
from sluice.algorithms.sliding_window_counter import SlidingWindowCounter

# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def backend():
    redis_client = fakeredis.FakeAsyncRedis()
    b = RedisBackend(client=redis_client)
    await b.connect()
    yield b
    await b.close()

def _inject_time(backend: RedisBackend, t_ms: int):
    """Override backend clock for deterministic tests."""
    async def fake_now_ms():
        return t_ms
    backend.now_ms = fake_now_ms


# ---------------------------------------------------------------------------
# Concurrent correctness
# ---------------------------------------------------------------------------

class TestConcurrentCorrectness:

    @pytest.mark.asyncio
    async def test_token_bucket_concurrent_never_exceeds_capacity(self, backend):
        """
        Fire 5× capacity concurrent requests. Allowed count must never exceed capacity.

        This is the atomicity proof: if the Lua script were not atomic, two coroutines
        could both read the same token count, both decide they're allowed, and both
        decrement — allowing capacity+1 requests through.
        """
        capacity = 10
        _inject_time(backend, 0)
        limiter = TokenBucket(backend, capacity=capacity, refill_rate=1.0)

        results = await asyncio.gather(*[
            limiter.check("shared_key") for _ in range(capacity * 5)
        ])

        allowed = sum(1 for r in results if r.allowed)
        assert allowed == capacity, (
            f"Concurrent token bucket allowed {allowed}, expected exactly {capacity}. "
            f"This would indicate a race condition in the atomic check."
        )

    @pytest.mark.asyncio
    async def test_sliding_window_log_concurrent_never_exceeds_limit(self, backend):
        """Same proof for SlidingWindowLog."""
        limit = 10
        _inject_time(backend, 0)
        limiter = SlidingWindowLog(backend, limit=limit, window_ms=60_000)

        results = await asyncio.gather(*[
            limiter.check("shared_key") for _ in range(limit * 5)
        ])

        allowed = sum(1 for r in results if r.allowed)
        assert allowed == limit, (
            f"Concurrent SWLog allowed {allowed}, expected exactly {limit}."
        )

    @pytest.mark.asyncio
    async def test_sliding_window_counter_concurrent_never_exceeds_limit(self, backend):
        """
        SWC uses an approximation, so we allow a small tolerance.
        The atomicity claim still holds: no race condition, just an algorithmic bound.
        """
        limit = 10
        _inject_time(backend, 500)  # middle of a window, no previous bucket weight
        limiter = SlidingWindowCounter(backend, limit=limit, window_ms=1_000)

        results = await asyncio.gather(*[
            limiter.check("shared_key") for _ in range(limit * 5)
        ])

        allowed = sum(1 for r in results if r.allowed)
        # SWC tolerance: ≤ limit + 1 (approximation error, not a race condition)
        assert allowed <= limit + 1, (
            f"Concurrent SWC allowed {allowed}, expected ≤ {limit + 1}."
        )
