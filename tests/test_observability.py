from __future__ import annotations

import asyncio

import pytest

from sluice.backends.fallback_backend import FallbackBackend
from sluice.domain import RateLimitOperation, TokenBucketConfig
from sluice.observability import (
    InstrumentedBackend, MetricsCollector, RateLimitEvent,
)


def operation(key="test-key"):
    return RateLimitOperation(
        algorithm="token_bucket",
        key=key,
        config=TokenBucketConfig(key_prefix="test", capacity=2, refill_rate=1.0),
        cost=1,
    )


def test_instrumented_backend_records_allowed_and_limited_without_keys():
    metrics = MetricsCollector()
    backend = InstrumentedBackend(FallbackBackend(), metrics)
    asyncio.run(backend.check(operation()))
    asyncio.run(backend.check(operation()))
    result = asyncio.run(backend.check(operation()))
    assert not result.allowed
    snapshot = metrics.snapshot()
    allowed = next(x for x in snapshot["counters"] if x["outcome"] == "allowed")
    limited = next(x for x in snapshot["counters"] if x["outcome"] == "limited")
    assert allowed["count"] == 2
    assert limited["count"] == 1
    assert "test-key" not in repr(snapshot)


def test_instrumented_backend_records_errors_and_reraises():
    class Broken:
        async def check(self, operation):
            raise RuntimeError("sensitive error message")

    metrics = MetricsCollector()
    backend = InstrumentedBackend(Broken(), metrics)
    with pytest.raises(RuntimeError):
        asyncio.run(backend.check(operation()))
    snapshot = metrics.snapshot()
    assert any(x["outcome"] == "error" for x in snapshot["counters"])
    assert "sensitive error" not in repr(snapshot)


def test_observer_failure_does_not_change_successful_admission():
    class BrokenObserver:
        def on_event(self, event):
            raise RuntimeError("exporter unavailable")

    backend = InstrumentedBackend(FallbackBackend(), BrokenObserver())
    result = asyncio.run(backend.check(operation()))
    assert result.allowed


def test_observer_failure_does_not_replace_backend_error():
    class BrokenBackend:
        async def check(self, operation):
            raise RuntimeError("backend unavailable")

    class BrokenObserver:
        def on_event(self, event):
            raise ValueError("observer unavailable")

    backend = InstrumentedBackend(BrokenBackend(), BrokenObserver())
    with pytest.raises(RuntimeError, match="backend unavailable"):
        asyncio.run(backend.check(operation()))


def test_metrics_dimensions_are_bounded():
    metrics = MetricsCollector()
    metrics.on_event(RateLimitEvent(
        name="attacker-controlled-name", algorithm="/private/path",
        backend="custom-backend-name", outcome="secret-outcome",
    ))
    snapshot = metrics.snapshot()
    assert snapshot["counters"][0]["event"] == "unknown"
    assert snapshot["counters"][0]["algorithm"] == "unknown"
    assert snapshot["counters"][0]["backend"] == "unknown"
    assert snapshot["counters"][0]["outcome"] == "unknown"


def test_metrics_collector_is_thread_safe():
    import threading

    metrics = MetricsCollector()
    event = RateLimitEvent(name="backend_fallback", backend="hybrid", outcome="fallback")
    threads = [threading.Thread(target=lambda: [metrics.on_event(event) for _ in range(500)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    item = metrics.snapshot()["counters"][0]
    assert item["count"] == 4000


def test_instrumented_backend_forwards_lifecycle_attributes():
    class Backend:
        marker = "preserved"

        async def check(self, operation):
            raise AssertionError

    wrapped = InstrumentedBackend(Backend(), MetricsCollector())
    assert wrapped.marker == "preserved"
