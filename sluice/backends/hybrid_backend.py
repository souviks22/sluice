"""
HybridBackend: circuit breaker wrapping RedisBackend.
 
The failure-mode problem
------------------------
A simple fail-open strategy (let everything through when Redis is down) defeats
the purpose of rate limiting during an outage — exactly when abuse is most likely
to spike. A fail-closed strategy (reject everything) takes down your service.
 
The real answer is a third option: fall back to *local* in-process limiting.
It's weaker than distributed limiting (each node enforces independently, so
N nodes allow N × limit), but it's far better than either extreme:
  - Legitimate traffic at normal volume still gets through.
  - A flood that would hit every node is still bounded per-node.
  - The degradation is visible in metrics and self-corrects when Redis recovers.

"""

from __future__ import annotations
 
import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
 
from sluice.backends.fallback_backend import FallbackBackend
from sluice.backends.redis_backend import RedisBackend
 
logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED    = auto()   # healthy — using Redis
    OPEN      = auto()   # degraded — using local fallback
    HALF_OPEN = auto()   # probing — one Redis call to test recovery


@dataclass
class HybridBackend:
    """
    Parameters
    ----------
    redis_url           : str            - URL of the Redis instance
    failure_threshold   : int            - consecutive Redis failures before opening circuit
    recovery_timeout_sec: float          - seconds to wait before probing Redis again
    local_maxsize       : int            - max distinct keys tracked per algorithm in
                                            the local fallback cache before LRU eviction
    local_ttl_sec       : float          - seconds an idle local entry is kept before
                                            it's automatically evicted
    """

    redis_url: str = "redis://localhost:6379/0"
    failure_threshold: int = 5
    recovery_timeout_sec: float = 30.0
    local_maxsize: int = 10_000
    local_ttl_sec: float = 300.0


    _state: CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _failure_count: int  = field(default=0, init=False, repr=False)
    _last_failure_ts: float = field(default=0.0, init=False, repr=False)
    _redis: RedisBackend = field(init=False, repr=False)
    _local: FallbackBackend = field(init=False, repr=False)
    _lock: asyncio.Lock = field(init=False, repr=False)


    def __post_init__(self) -> None:
        self._redis = RedisBackend.from_url(self.redis_url)
        self._local = FallbackBackend(maxsize=self.local_maxsize, ttl=self.local_ttl_sec)
        self._lock  = asyncio.Lock()


    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
 
    async def connect(self) -> None:
        await self._redis.connect()
 
    async def close(self) -> None:
        await self._redis.close()
 
    async def ping(self) -> bool:
        return await self._redis.ping()


    # ------------------------------------------------------------------
    # Circuit breaker logic
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        return self._state
 
    @property
    def is_degraded(self) -> bool:
        return self._state != CircuitState.CLOSED
 
    def local_cache_size(self) -> dict[str, int]:
        """Expose local fallback cache occupancy, e.g. for metrics export."""
        return self._local.current_size()

    async def _try_recover(self) -> bool:
        """
        Called when circuit is OPEN and recovery_timeout has elapsed.
        Returns True if Redis responded — transition to CLOSED.
        """
        try:
            redis_ok = await self._redis.ping()
            if redis_ok:
                async with self._lock:
                    self._state         = CircuitState.CLOSED
                    self._failure_count = 0
                logger.info("sluice: circuit breaker CLOSED — Redis recovered")
                return True
            return False
        except Exception:
            return False

    def _record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_ts = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            if self._state == CircuitState.CLOSED:
                logger.warning(
                    "sluice: circuit breaker OPEN after %d consecutive Redis failures. "
                    "Falling back to local in-process limiting. "
                    "NOTE: cluster-wide limit is now N × configured_limit "
                    "where N = number of running instances.",
                    self._failure_count,
                )
            self._state = CircuitState.OPEN


    # ------------------------------------------------------------------
    # Core dispatch
    # ------------------------------------------------------------------

    async def _route(self, redis_call: Any, local_call: Any) -> Any:
        """
        redis_call/local_call: zero-arg async callables. Returns whichever
        one actually served the request, per current circuit state.
        """
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_ts
            if elapsed < self.recovery_timeout_sec:
                return await local_call()
            # Transition to HALF_OPEN and send one probe
            self._state = CircuitState.HALF_OPEN
            recovered   = await self._try_recover()
            if not recovered:
                self._last_failure_ts = time.monotonic()
                return await local_call()
 
        if self._state == CircuitState.HALF_OPEN:
            # Still waiting for probe result — use local
            return await local_call()
 
        try:
            result = await redis_call()
            self._failure_count = 0
            return result
        except Exception as exc:
            self._record_failure()
            logger.debug("sluice: Redis error (failure %d/%d): %s",
                         self._failure_count, self.failure_threshold, exc)
            return await local_call()

    async def now_ms(self) -> int:
        return await self._route(
            redis_call=self._redis.now_ms,
            local_call=self._local.now_ms,
        )

    async def evalsha(self, script_name: str, keys: list[str], args: list[Any]) -> list[Any]:
        return await self._route(
            redis_call=lambda: self._redis.evalsha(script_name, keys, args),
            local_call=lambda: self._local.evalsha(script_name, keys, args),
        )