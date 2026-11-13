"""Dependency-free observability primitives for Sluice.

Events intentionally omit identifiers, Redis keys, request paths, and raw
exception messages. Metric dimensions are normalized to bounded vocabularies.
"""

from __future__ import annotations

import threading
import time
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from sluice.domain import RateLimitOperation, RateLimitResult


_ALGORITHMS = {
    "token_bucket", "sliding_window_log", "sliding_window_counter", "unknown"
}
_BACKENDS = {"redis", "fallback", "hybrid", "custom"}
_OUTCOMES = {"allowed", "limited", "error", "fallback", "success", "failure"}
_STATES = {"closed", "open", "half_open", "unknown"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitEvent:
    """One low-cardinality operational event."""

    name: str
    algorithm: str = "unknown"
    backend: str = "custom"
    outcome: str = "success"
    duration_seconds: float | None = None
    state: str = "unknown"


class RateLimitObserver(Protocol):
    """Synchronous, non-blocking event sink contract."""

    def on_event(self, event: RateLimitEvent) -> None: ...


def notify_observer(observer: RateLimitObserver, event: RateLimitEvent) -> None:
    """Deliver an event without letting instrumentation alter enforcement."""
    try:
        observer.on_event(event)
    except Exception as exc:
        # Do not include the exception message: observers may embed sensitive
        # exporter configuration or event data in it.
        try:
            logger.warning(
                "Sluice observer raised %s; dropping observability event",
                type(exc).__name__,
            )
        except Exception:
            # A custom logging handler must not reintroduce an enforcement
            # failure while reporting an observer failure.
            pass


class NoOpObserver:
    """Default observer; useful when instrumentation is optional."""

    def on_event(self, event: RateLimitEvent) -> None:
        return None


def _bounded(value: str, allowed: set[str]) -> str:
    return value if value in allowed else "unknown"


def _backend_name(backend: Any) -> str:
    name = type(backend).__name__.lower()
    if "hybrid" in name:
        return "hybrid"
    if "redis" in name:
        return "redis"
    if "fallback" in name or "memory" in name or "local" in name:
        return "fallback"
    return "custom"


class MetricsCollector:
    """Thread-safe aggregate counters and latency summaries.

    No per-key or per-route labels are retained. ``snapshot`` returns plain
    data suitable for adapting to Prometheus, StatsD, or an OTLP exporter.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[tuple[str, str, str, str]] = Counter()
        self._latency: dict[tuple[str, str], list[float]] = {}

    def on_event(self, event: RateLimitEvent) -> None:
        name = event.name if event.name in {
            "admission", "backend_fallback", "circuit_state", "backend_error"
        } else "unknown"
        algorithm = _bounded(event.algorithm, _ALGORITHMS)
        backend = _bounded(event.backend, _BACKENDS)
        outcome = _bounded(event.outcome, _OUTCOMES)
        state = _bounded(event.state, _STATES)
        with self._lock:
            self._counters[(name, algorithm, backend, outcome)] += 1
            if event.duration_seconds is not None:
                duration = max(0.0, float(event.duration_seconds))
                values = self._latency.setdefault((algorithm, backend), [0.0, 0.0, 0.0])
                values[0] += 1
                values[1] += duration
                values[2] = max(values[2], duration)
            if name == "circuit_state":
                self._counters[(name, algorithm, backend, state)] += 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = [
                {
                    "event": key[0], "algorithm": key[1],
                    "backend": key[2], "outcome": key[3], "count": count,
                }
                for key, count in sorted(self._counters.items())
            ]
            latency = [
                {
                    "algorithm": key[0], "backend": key[1],
                    "count": int(value[0]), "total_seconds": value[1],
                    "max_seconds": value[2],
                }
                for key, value in sorted(self._latency.items())
            ]
        return {"counters": counters, "latency": latency}


class InstrumentedBackend:
    """Decorator that instruments the complete backend admission operation.

    The observer is synchronous and should be fast/non-blocking. Exporters
    that perform I/O should enqueue events rather than blocking requests.
    """

    def __init__(self, backend: Any, observer: RateLimitObserver) -> None:
        self.backend = backend
        self.observer = observer
        self.backend_name = _backend_name(backend)

    async def check(self, operation: RateLimitOperation) -> RateLimitResult:
        started = time.perf_counter()
        try:
            result = await self.backend.check(operation)
        except Exception:
            notify_observer(self.observer, RateLimitEvent(
                name="admission", algorithm=operation.algorithm,
                backend=self.backend_name, outcome="error",
                duration_seconds=time.perf_counter() - started,
            ))
            raise
        notify_observer(self.observer, RateLimitEvent(
            name="admission", algorithm=operation.algorithm,
            backend=self.backend_name,
            outcome="allowed" if result.allowed else "limited",
            duration_seconds=time.perf_counter() - started,
        ))
        return result

    def __getattr__(self, name: str) -> Any:
        # Preserve optional lifecycle/health/introspection APIs.
        return getattr(self.backend, name)
