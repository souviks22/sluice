from __future__ import annotations

import pytest

from sluice.backends import FallbackBackend
from sluice.backends.hybrid_backend import CircuitState, HybridBackend
from sluice.domain import (
    BackendUnavailableError,
    RateLimitConfigurationError,
    RateLimitOperation,
    RateLimitResult,
    TokenBucketConfig,
)


def op():
    return RateLimitOperation(
        algorithm="token_bucket", key="test:hybrid",
        config=TokenBucketConfig(key_prefix="test", capacity=5, refill_rate=1.0),
        cost=2,
    )


class FakeRedis:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0

    async def check(self, operation):
        self.calls += 1
        if self.fail:
            raise TimeoutError("simulated Redis timeout")
        return RateLimitResult(True, 3, 1000, 0, operation.algorithm, operation.key)


class FakeLocal:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0

    async def check(self, operation):
        self.calls += 1
        if self.fail:
            raise RuntimeError("local unavailable")
        return RateLimitResult(True, 3, 1000, 0, operation.algorithm, operation.key)

    def current_size(self):
        return {"token_bucket": 1}


@pytest.mark.asyncio
async def test_redis_timeout_falls_back_for_whole_operation():
    redis = FakeRedis(fail=True)
    local = FakeLocal()
    backend = HybridBackend(
        failure_threshold=1, redis_call_timeout_sec=0.05,
        recovery_timeout_sec=60, recovery_jitter_frac=0,
        redis_backend=redis, fallback_backend=local,
    )
    result = await backend.check(op())
    assert result.allowed
    assert redis.calls == 1
    assert local.calls == 1
    assert backend.state == CircuitState.OPEN
    assert backend.is_degraded


@pytest.mark.asyncio
async def test_observer_failures_do_not_interrupt_hybrid_fallback():
    class BrokenObserver:
        def on_event(self, event):
            raise RuntimeError("metrics exporter unavailable")

    redis = FakeRedis(fail=True)
    local = FakeLocal()
    backend = HybridBackend(
        failure_threshold=1,
        recovery_timeout_sec=60,
        redis_backend=redis,
        fallback_backend=local,
        observer=BrokenObserver(),
    )

    result = await backend.check(op())

    assert result.allowed
    assert redis.calls == 1
    assert local.calls == 1
    assert backend.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_open_circuit_routes_directly_to_local():
    redis = FakeRedis(fail=True)
    local = FakeLocal()
    backend = HybridBackend(
        failure_threshold=1, recovery_timeout_sec=60, recovery_jitter_frac=0,
        redis_backend=redis, fallback_backend=local,
    )
    await backend.check(op())
    await backend.check(op())
    assert redis.calls == 1
    assert local.calls == 2


@pytest.mark.asyncio
async def test_half_open_probe_recovers_and_closes_circuit():
    redis = FakeRedis(fail=True)
    local = FakeLocal()
    backend = HybridBackend(
        failure_threshold=1, redis_call_timeout_sec=0.05,
        recovery_timeout_sec=0, recovery_jitter_frac=0,
        redis_backend=redis, fallback_backend=local,
    )
    await backend.check(op())
    assert backend.state == CircuitState.OPEN
    redis.fail = False
    result = await backend.check(op())
    assert result.allowed
    assert redis.calls == 2
    assert local.calls == 1
    assert backend.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_fallback_failure_propagates_instead_of_fail_open():
    backend = HybridBackend(
        failure_threshold=1, recovery_timeout_sec=60,
        redis_backend=FakeRedis(fail=True), fallback_backend=FakeLocal(fail=True),
    )
    with pytest.raises(RuntimeError, match="local unavailable"):
        await backend.check(op())


@pytest.mark.asyncio
async def test_fail_closed_policy_does_not_consume_local_quota_after_redis_error():
    redis = FakeRedis(fail=True)
    local = FakeLocal()
    backend = HybridBackend(
        failure_threshold=1,
        redis_backend=redis,
        fallback_backend=local,
        failure_policy="fail_closed",
    )

    with pytest.raises(BackendUnavailableError, match="admission failed"):
        await backend.check(op())
    assert redis.calls == 1
    assert local.calls == 0

    # While open, fail closed without another Redis request or local decision.
    with pytest.raises(BackendUnavailableError, match="circuit is open"):
        await backend.check(op())
    assert redis.calls == 1
    assert local.calls == 0


def test_hybrid_failure_policy_rejects_unknown_values():
    with pytest.raises(RateLimitConfigurationError):
        HybridBackend(failure_policy="fail_open")


@pytest.mark.asyncio
async def test_hybrid_exposes_local_capacity_evictions():
    local = FallbackBackend(maxsize=1)
    backend = HybridBackend(
        failure_threshold=1,
        recovery_timeout_sec=60,
        redis_backend=FakeRedis(fail=True),
        fallback_backend=local,
    )
    first = op()
    second = RateLimitOperation(
        algorithm=first.algorithm,
        key="test:hybrid:second",
        config=first.config,
        cost=first.cost,
    )

    await backend.check(first)
    await backend.check(second)
    assert backend.local_cache_evictions() == {
        "token_bucket": 1,
        "sliding_window_log": 0,
        "sliding_window_counter": 0,
    }
