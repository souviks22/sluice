"""
HybridBackend: circuit breaker wrapping RedisBackend.

The failure-mode problem
------------------------
A simple fail-open strategy (let everything through when Redis is down) defeats
the purpose of rate limiting during an outage — exactly when abuse is most likely
to spike. A fail-closed strategy (reject everything) takes down your service.

The real answer is a third option: fall back to *local* in-process limiting.
It's weaker than distributed limiting (each node enforces independently, so
N nodes allow N x limit), but it's far better than either extreme:
  - Legitimate traffic at normal volume still gets through.
  - A flood that would hit every node is still bounded per-node.
  - The degradation is visible in metrics and self-corrects when Redis recovers.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Awaitable, Callable, Literal

from sluice.backends.fallback_backend import FallbackBackend
from sluice.domain import (
    BackendUnavailableError,
    RateLimitConfigurationError,
    RateLimitOperation,
    RateLimitResult,
)
from sluice.observability import (
    NoOpObserver,
    RateLimitEvent,
    RateLimitObserver,
    notify_observer,
)

logger = logging.getLogger(__name__)


def _validate_circuit_settings(
    failure_threshold: int,
    recovery_timeout_sec: float,
    recovery_jitter_frac: float,
) -> None:
    if (
        isinstance(failure_threshold, bool)
        or not isinstance(failure_threshold, int)
        or failure_threshold <= 0
    ):
        raise RateLimitConfigurationError("failure_threshold must be a positive integer.")
    if (
        isinstance(recovery_timeout_sec, bool)
        or not isinstance(recovery_timeout_sec, (int, float))
        or not math.isfinite(recovery_timeout_sec)
        or recovery_timeout_sec < 0
    ):
        raise RateLimitConfigurationError(
            "recovery_timeout_sec must be a non-negative finite number."
        )
    if (
        isinstance(recovery_jitter_frac, bool)
        or not isinstance(recovery_jitter_frac, (int, float))
        or not math.isfinite(recovery_jitter_frac)
        or not 0 <= recovery_jitter_frac < 1
    ):
        raise RateLimitConfigurationError(
            "recovery_jitter_frac must be in the range [0, 1)."
        )


class CircuitState(Enum):
    CLOSED    = auto()   # healthy — using Redis
    OPEN      = auto()   # degraded — using local fallback
    HALF_OPEN = auto()   # probing — one Redis call in flight to test recovery


# ---------------------------------------------------------------------------
# Pure state machine — no I/O, no locks, no awaits.
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreaker:
    """
    Tracks circuit state and decides whether a given call should go to
    Redis or to the local fallback. Contains zero async code by design:
    every public method here runs synchronously start-to-finish, which is
    what makes it safe under concurrent callers without a lock.

    Parameters
    ----------
    failure_threshold    : consecutive Redis failures before opening the circuit
    recovery_timeout_sec : base seconds to wait before probing Redis again
    recovery_jitter_frac : +/- fraction of recovery_timeout_sec to randomize,
                            so concurrent instances don't re-probe in lockstep;
                            must be in [0, 1)
    """

    failure_threshold: int = 5
    recovery_timeout_sec: float = 30.0
    recovery_jitter_frac: float = 0.2

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_transition_ts: float = field(default_factory=time.monotonic, init=False)
    _current_recovery_deadline_sec: float = field(default=0.0, init=False)
    _generation: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        _validate_circuit_settings(
            self.failure_threshold,
            self.recovery_timeout_sec,
            self.recovery_jitter_frac,
        )

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_degraded(self) -> bool:
        return self._state != CircuitState.CLOSED

    @property
    def generation(self) -> int:
        return self._generation

    def _jittered_timeout(self) -> float:
        spread = self.recovery_timeout_sec * self.recovery_jitter_frac
        return self.recovery_timeout_sec + random.uniform(-spread, spread)

    def _transition(self, new_state: CircuitState) -> None:
        if new_state == self._state:
            return
        old_state = self._state
        self._state = new_state
        self._last_transition_ts = time.monotonic()
        self._generation += 1

        if new_state == CircuitState.OPEN:
            self._current_recovery_deadline_sec = self._jittered_timeout()
            if old_state == CircuitState.CLOSED:
                logger.warning(
                    "sluice: circuit breaker OPEN after %d consecutive Redis "
                    "failures. Falling back to local in-process limiting. "
                    "NOTE: cluster-wide limit is now N x configured_limit "
                    "where N = number of running instances.",
                    self._failure_count,
                )
            elif old_state == CircuitState.HALF_OPEN:
                logger.warning("sluice: recovery probe failed — circuit OPEN again")
        elif new_state == CircuitState.CLOSED:
            self._failure_count = 0
            logger.info("sluice: circuit breaker CLOSED — Redis recovered")
        elif new_state == CircuitState.HALF_OPEN:
            logger.info("sluice: circuit breaker HALF_OPEN — probing Redis")

    def should_use_redis(self) -> tuple[bool, int]:
        """
        Decide routing for one call. Returns (use_redis, generation) — the
        caller must pass `generation` back to record_success/record_failure
        unchanged, so stale completions can be detected and dropped.
        """
        if self._state == CircuitState.CLOSED:
            return True, self._generation

        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_transition_ts
            if elapsed < self._current_recovery_deadline_sec:
                return False, self._generation
            # Timeout elapsed: this call becomes the single recovery probe.
            self._transition(CircuitState.HALF_OPEN)
            return True, self._generation

        # HALF_OPEN: a probe is already in flight (or was just decided above
        # in this same synchronous call) — no other caller should probe too.
        return False, self._generation

    def record_success(self, generation: int) -> None:
        if generation != self._generation:
            return  # stale completion from a since-superseded generation
        self._failure_count = 0
        if self._state != CircuitState.CLOSED:
            self._transition(CircuitState.CLOSED)

    def record_failure(self, generation: int) -> None:
        if generation != self._generation:
            return  # stale completion — ignore
        if self._state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN)  # the probe itself failed
            return
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN)


# ---------------------------------------------------------------------------
# Async wrapper — thin. All it does is execute calls and report outcomes
# back to the breaker.
# ---------------------------------------------------------------------------

@dataclass
class HybridBackend:
    """Redis admission with bounded process-local fallback on Redis errors.

    ``failure_policy="local_fallback"`` falls back locally after Redis errors
    and timeouts. ``failure_policy="fail_closed"`` propagates a typed backend
    error instead, including while the circuit is open. The default preserves
    historical local-fallback behavior. A timed-out Redis command may already
    have committed, so local fallback cannot guarantee exactly-once admission.

    Parameters
    ----------
    redis_url             : URL of the Redis instance
    failure_threshold      : consecutive Redis failures before opening circuit
    recovery_timeout_sec    : base seconds to wait before probing Redis again
    recovery_jitter_frac   : jitter fraction applied to recovery_timeout_sec
    redis_call_timeout_sec : per-call timeout; a hanging Redis is treated as
                              a failure, not left to block indefinitely
    local_maxsize          : max distinct keys tracked per algorithm in the
                              local fallback cache before LRU eviction
    local_ttl_sec          : seconds an idle local entry is kept before
                              automatic eviction
    failure_policy         : ``"local_fallback"`` or ``"fail_closed"``

    ``failure_threshold`` must be a positive integer. The recovery timeout may
    be zero for immediate probing; the Redis call timeout must be positive.
    """

    redis_url: str = "redis://localhost:6379/0"
    failure_threshold: int = 5
    recovery_timeout_sec: float = 30.0
    recovery_jitter_frac: float = 0.2
    redis_call_timeout_sec: float = 0.5
    local_maxsize: int = 10_000
    local_ttl_sec: float = 300.0
    failure_policy: Literal["local_fallback", "fail_closed"] = "local_fallback"
    observer: RateLimitObserver = field(default_factory=NoOpObserver, repr=False)
    redis_backend: Any | None = field(default=None, repr=False)
    fallback_backend: Any | None = field(default=None, repr=False)

    _redis: Any = field(init=False, repr=False)
    _local: Any = field(init=False, repr=False)
    _breaker: CircuitBreaker = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_circuit_settings(
            self.failure_threshold,
            self.recovery_timeout_sec,
            self.recovery_jitter_frac,
        )
        if self.failure_policy not in ("local_fallback", "fail_closed"):
            raise RateLimitConfigurationError(
                "failure_policy must be 'local_fallback' or 'fail_closed'."
            )
        if (
            isinstance(self.redis_call_timeout_sec, bool)
            or not isinstance(self.redis_call_timeout_sec, (int, float))
            or not math.isfinite(self.redis_call_timeout_sec)
            or self.redis_call_timeout_sec <= 0
        ):
            raise RateLimitConfigurationError(
                "redis_call_timeout_sec must be a positive finite number."
            )
        # Keep the hybrid policy/circuit-breaker importable without the optional
        # Redis client and allow deterministic backend injection in tests.
        if self.redis_backend is None:
            from sluice.backends.redis_backend import RedisBackend
            self._redis = RedisBackend.from_url(self.redis_url)
        else:
            self._redis = self.redis_backend
        self._local = self.fallback_backend or FallbackBackend(
            maxsize=self.local_maxsize, ttl=self.local_ttl_sec
        )
        self._breaker = CircuitBreaker(
            failure_threshold=self.failure_threshold,
            recovery_timeout_sec=self.recovery_timeout_sec,
            recovery_jitter_frac=self.recovery_jitter_frac,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        await self._redis.connect()

    async def close(self) -> None:
        await self._redis.close()

    async def ping(self) -> bool:
        try:
            return bool(await asyncio.wait_for(
                self._redis.ping(), timeout=self.redis_call_timeout_sec
            ))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Circuit breaker status (delegated — kept here for a stable public API)
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        return self._breaker.state

    @property
    def is_degraded(self) -> bool:
        return self._breaker.is_degraded

    def local_cache_size(self) -> dict[str, int]:
        """Expose local fallback cache occupancy, e.g. for metrics export."""
        return self._local.current_size()

    def local_cache_evictions(self) -> dict[str, int]:
        """Expose local fallback capacity evictions that may reset quotas."""
        get_counts = getattr(self._local, "eviction_counts", None)
        if get_counts is None:
            return {}
        return get_counts()

    # ------------------------------------------------------------------
    # Core dispatch
    # ------------------------------------------------------------------

    async def _route(
        self,
        redis_call: Callable[[], Awaitable[Any]],
        local_call: Callable[[], Awaitable[Any]],
        op_name: str,
    ) -> Any:
        use_redis, generation = self._breaker.should_use_redis()

        if not use_redis and self.failure_policy == "local_fallback":
            notify_observer(self.observer, RateLimitEvent(
                name="backend_fallback", backend="hybrid", outcome="fallback",
            ))
        elif not use_redis:
            raise BackendUnavailableError(
                "Redis circuit is open; hybrid failure_policy='fail_closed'."
            )

        if use_redis:
            try:
                result = await asyncio.wait_for(
                    redis_call(), timeout=self.redis_call_timeout_sec
                )
                previous_state = self._breaker.state
                self._breaker.record_success(generation)
                if self._breaker.state != previous_state:
                    notify_observer(self.observer, RateLimitEvent(
                        name="circuit_state", backend="hybrid",
                        outcome="success", state=self._breaker.state.name.lower(),
                    ))
                return result
            except Exception as exc:
                previous_state = self._breaker.state
                self._breaker.record_failure(generation)
                if self._breaker.state != previous_state:
                    notify_observer(self.observer, RateLimitEvent(
                        name="circuit_state", backend="hybrid",
                        outcome="failure", state=self._breaker.state.name.lower(),
                    ))
                notify_observer(self.observer, RateLimitEvent(
                    name="backend_error", backend="redis", outcome="failure",
                ))
                if self.failure_policy == "fail_closed":
                    raise BackendUnavailableError(
                        "Redis admission failed; hybrid failure_policy='fail_closed'."
                    ) from exc
                notify_observer(self.observer, RateLimitEvent(
                    name="backend_fallback", backend="hybrid", outcome="fallback",
                ))
                logger.debug(
                    "sluice: Redis error on %s (gen %d): %s", op_name, generation, exc
                )
                # fall through to local

        try:
            return await local_call()
        except Exception:
            logger.exception(
                "sluice: local fallback for %s also failed — denying by default",
                op_name,
            )
            raise

    async def check(self, operation: RateLimitOperation) -> RateLimitResult:
        """Route the entire admission operation through one backend selection."""
        return await self._route(
            redis_call=lambda: self._redis.check(operation),
            local_call=lambda: self._local.check(operation),
            op_name=f"check:{operation.algorithm}",
        )

    async def now_ms(self) -> int:
        return await self._route(
            redis_call=self._redis.now_ms,
            local_call=self._local.now_ms,
            op_name="now_ms",
        )

    async def evalsha(self, script_name: str, keys: list[str], args: list[Any]) -> list[Any]:
        return await self._route(
            redis_call=lambda: self._redis.evalsha(script_name, keys, args),
            local_call=lambda: self._local.evalsha(script_name, keys, args),
            op_name=f"evalsha:{script_name}",
        )
    
