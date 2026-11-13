"""Bounded, process-local fallback backend.

This backend is deliberately not distributed: each process maintains its own
quota state. Every admission transition is serialized under one process lock,
including weighted requests, so a rejected weighted request never partially
consumes quota. TTL expiry and LRU eviction bound memory; eviction can reset a
key's quota early and is therefore a documented degradation trade-off.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Generic, TypeVar

from cachetools import TTLCache

from sluice.domain import RateLimitOperation, RateLimitResult, SlidingWindowConfig, TokenBucketConfig
from sluice.domain.exceptions import RateLimitConfigurationError

_Key = TypeVar("_Key")
_Value = TypeVar("_Value")


class _EvictionTrackingTTLCache(TTLCache[_Key, _Value], Generic[_Key, _Value]):
    """TTL/LRU cache that counts capacity evictions separately from expiry."""

    def __init__(self, maxsize: int, ttl: float) -> None:
        super().__init__(maxsize=maxsize, ttl=ttl)
        self.capacity_evictions = 0

    def popitem(self) -> tuple[_Key, _Value]:
        item = super().popitem()
        self.capacity_evictions += 1
        return item


class FallbackBackend:
    """Atomic in-process limiter used only when the primary backend is unavailable.

    ``maxsize`` is the maximum number of tracked keys per algorithm cache.
    ``ttl`` is the idle lifetime in seconds. LRU eviction may reset a key's
    quota before TTL; this is preferable to unbounded memory growth but weakens
    enforcement under high-cardinality key churn.
    """

    def __init__(self, maxsize: int = 10_000, ttl: float = 300.0) -> None:
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize <= 0:
            raise RateLimitConfigurationError("maxsize must be a positive integer")
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
            raise RateLimitConfigurationError("ttl must be a positive finite number")
        self._token_bucket_cache: _EvictionTrackingTTLCache[str, dict[str, float]] = _EvictionTrackingTTLCache(
            maxsize=maxsize, ttl=float(ttl)
        )
        self._sliding_log_cache: _EvictionTrackingTTLCache[str, list[tuple[float, str]]] = _EvictionTrackingTTLCache(
            maxsize=maxsize, ttl=float(ttl)
        )
        self._sliding_counter_cache: _EvictionTrackingTTLCache[str, dict[str, float]] = _EvictionTrackingTTLCache(
            maxsize=maxsize, ttl=float(ttl)
        )
        self._lock = threading.RLock()

    async def now_ms(self) -> int:
        """Legacy compatibility method; new operations use check()."""
        return int(time.time() * 1000)

    async def check(self, operation: RateLimitOperation) -> RateLimitResult:
        """Admit a complete operation atomically within this process."""
        # No await occurs inside the critical section: asyncio tasks cannot
        # interleave, while the threading lock also protects multi-threaded ASGI.
        now_ms = await self.now_ms()
        with self._lock:
            config = operation.config
            if operation.algorithm == "token_bucket":
                assert isinstance(config, TokenBucketConfig)
                raw = self._token_bucket(
                    operation.key, config.capacity, config.refill_rate,
                    operation.cost, now_ms,
                )
            elif operation.algorithm == "sliding_window_log":
                assert isinstance(config, SlidingWindowConfig)
                raw = self._sliding_window_log_weighted(
                    operation.key, config.limit, config.window_ms,
                    operation.cost, now_ms,
                )
            elif operation.algorithm == "sliding_window_counter":
                assert isinstance(config, SlidingWindowConfig)
                raw = self._sliding_window_counter_weighted(
                    operation.key, config.limit, config.window_ms,
                    operation.cost, now_ms,
                )
            else:  # RateLimitOperation validates this; defensive for extensions.
                raise RateLimitConfigurationError(
                    f"Unsupported algorithm: {operation.algorithm!r}"
                )

        return RateLimitResult(
            allowed=bool(raw[0]), remaining=int(raw[1]),
            reset_after_ms=int(raw[2]), retry_after_ms=int(raw[3]),
            algorithm=operation.algorithm, key=operation.key,
        )

    def _token_bucket(
        self, key: str, capacity: int, refill_rate: float, cost: int, now_ms: int
    ) -> list[int]:
        bucket = self._token_bucket_cache.get(key)
        tokens = float(bucket["tokens"]) if bucket else float(capacity)
        last_refill = float(bucket["last_refill_ms"]) if bucket else float(now_ms)
        now_ms = max(now_ms, int(last_refill))
        elapsed = max(0.0, (now_ms - last_refill) / 1000.0)
        available = min(float(capacity), tokens + elapsed * refill_rate)
        allowed = int(available >= cost)
        retry_after = 0
        if allowed:
            available -= cost
        else:
            retry_after = math.ceil(((cost - available) / refill_rate) * 1000)
        self._token_bucket_cache[key] = {"tokens": available, "last_refill_ms": float(now_ms)}
        remaining = math.floor(available)
        reset_after = math.ceil(((capacity - available) / refill_rate) * 1000)
        return [allowed, remaining, reset_after, retry_after]

    def _sliding_window_log_weighted(
        self, key: str, limit: int, window_ms: int, cost: int, now_ms: int
    ) -> list[int]:
        cutoff = now_ms - window_ms
        log = sorted(
            (score, member)
            for score, member in self._sliding_log_cache.get(key, [])
            if score > cutoff
        )
        count = len(log)
        allowed = int(count + cost <= limit)
        retry_after = 0
        if allowed:
            for offset in range(cost):
                # The active cardinality is monotonic for this admission and
                # makes same-millisecond members unique without a second cache.
                log.append((float(now_ms), f"{now_ms}:{count + offset + 1}"))
        elif log:
            expirations_needed = count + cost - limit
            retry_after = max(
                0,
                math.ceil(log[expirations_needed - 1][0] + window_ms - now_ms),
            )
        self._sliding_log_cache[key] = log
        remaining = max(0, limit - count - (cost if allowed else 0))
        return [allowed, remaining, window_ms, retry_after]

    def _sliding_window_counter_weighted(
        self, key: str, limit: int, window_ms: int, cost: int, now_ms: int
    ) -> list[int]:
        bucket_id = math.floor(now_ms / window_ms)
        state = self._sliding_counter_cache.get(key)
        if state is None:
            stored_bucket, current, previous = bucket_id, 0.0, 0.0
        else:
            stored_bucket = int(state["bucket_id"])
            current, previous = state["current"], state["previous"]
            if bucket_id > stored_bucket:
                previous = current if bucket_id == stored_bucket + 1 else 0.0
                current = 0.0
                stored_bucket = bucket_id
            elif bucket_id < stored_bucket:
                bucket_id = stored_bucket
                now_ms = stored_bucket * window_ms

        position = (now_ms - bucket_id * window_ms) / window_ms
        overlap = 1.0 - position
        effective = previous * overlap + current
        allowed = int(effective + cost <= limit)
        retry_after = 0
        if allowed:
            current += cost
        else:
            remaining_capacity = limit - cost - current
            if previous > 0 and remaining_capacity >= 0:
                target_overlap = remaining_capacity / previous
                target_position = 1.0 - target_overlap
                retry_after = max(0, math.ceil((target_position - position) * window_ms))
            elif current > 0 and remaining_capacity < 0:
                retry_after = math.ceil(
                    (1.0 - position) * window_ms
                    + (1.0 - (limit - cost) / current) * window_ms
                )
            else:
                retry_after = math.ceil((1.0 - position) * window_ms)

        effective_after = previous * overlap + current
        remaining = max(0, math.floor(limit - effective_after))
        reset_after = math.ceil((1.0 - position) * window_ms)
        self._sliding_counter_cache[key] = {
            "bucket_id": float(stored_bucket),
            "current": current,
            "previous": previous,
        }
        return [allowed, remaining, reset_after, retry_after]

    # Legacy script interface retained for existing integrations. It is
    # intentionally not used by the operation-oriented algorithms.
    async def evalsha(self, script_name: str, keys: list[str], args: list[Any]) -> list[Any]:
        with self._lock:
            if script_name == "token_bucket":
                return self._legacy_token_bucket(keys, args)
            if script_name == "sliding_window_log":
                return self._legacy_sliding_window_log(keys, args)
            if script_name == "sliding_window_counter":
                return self._legacy_sliding_window_counter(keys, args)
            raise KeyError(f"Unknown script: {script_name}")

    def _legacy_token_bucket(self, keys: list[str], args: list[Any]) -> list[int]:
        return self._token_bucket(keys[0], int(args[0]), float(args[1]), int(args[2]), int(args[3]))

    def _legacy_sliding_window_log(self, keys: list[str], args: list[Any]) -> list[int]:
        return self._sliding_window_log_weighted(keys[0], int(args[0]), int(args[1]), 1, int(args[2]))

    def _legacy_sliding_window_counter(self, keys: list[str], args: list[Any]) -> list[int]:
        return self._sliding_window_counter_weighted(keys[0], int(args[0]), int(args[1]), 1, int(args[2]))

    def estimate_memory_bytes(self, prefix: str = "rl:") -> int:
        """Return a rough estimate for keys matching ``prefix``."""
        with self._lock:
            total = 0
            for cache in (self._token_bucket_cache, self._sliding_log_cache,
                          self._sliding_counter_cache):
                for key, value in list(cache.items()):
                    if not key.startswith(prefix):
                        continue
                    total += len(key) + len(repr(value)) + 64
            return total

    def current_size(self) -> dict[str, int]:
        """Expose per-algorithm cache occupancy for metrics."""
        with self._lock:
            return {
                "token_bucket": len(self._token_bucket_cache),
                "sliding_window_log": len(self._sliding_log_cache),
                "sliding_window_counter": len(self._sliding_counter_cache),
            }

    def eviction_counts(self) -> dict[str, int]:
        """Return capacity-eviction totals, excluding normal TTL expiration."""
        with self._lock:
            return {
                "token_bucket": self._token_bucket_cache.capacity_evictions,
                "sliding_window_log": self._sliding_log_cache.capacity_evictions,
                "sliding_window_counter": self._sliding_counter_cache.capacity_evictions,
            }
