"""
FallbackBackend: bounded, self-expiring local in-memory backend to survive 
Redis outage without leaking memory.
 
  - `TTLCache(maxsize, ttl)` per algorithm — idle entries expire
    automatically after `ttl` seconds, and `maxsize` puts a hard ceiling on
    memory with LRU eviction once full.
  - No persistence, no distributed semantics, no attempt to be a general
    limiter implementation — it exists only to be swapped in by
    HybridBackend while the circuit is OPEN, and discarded once Redis
    recovers.
"""

from __future__ import annotations
 
import math
import threading
import time
from typing import Any
 
from cachetools import TTLCache


class FallbackBackend:
    """
    Implements the same `evalsha(script_name, keys, args)` interface as
    RedisBackend, so HybridBackend can swap it in transparently during an outage
 
    Parameters
    ----------
    maxsize : int
        Max number of distinct entries held per algorithm's cache before
        LRU eviction kicks in.
    ttl : float
        Seconds an idle entry is kept before being evicted automatically.
    """
 
    def __init__(self, maxsize: int = 10_000, ttl: float = 300.0) -> None:
        self._token_bucket_cache: TTLCache[str, dict] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._sliding_log_cache: TTLCache[str, list] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._sliding_log_seq_cache: TTLCache[str, int] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._sliding_counter_cache: TTLCache[str, float] = TTLCache(maxsize=maxsize * 2, ttl=ttl)
        self._lock = threading.Lock()


    async def now_ms(self) -> int:
        return int(time.time() * 1000)

    # ------------------------------------------------------------------
    # Core dispatch
    # ------------------------------------------------------------------
 
    async def evalsha(self, script_name: str, keys: list[str], args: list[Any]) -> list[Any]:
        with self._lock:
            if script_name == "token_bucket":
                return self._token_bucket(keys, args)
            elif script_name == "sliding_window_log":
                return self._sliding_window_log(keys, args)
            elif script_name == "sliding_window_counter":
                return self._sliding_window_counter(keys, args)
            else:
                raise KeyError(f"Unknown script: {script_name}")


    # ------------------------------------------------------------------
    # Token bucket (mirrors token_bucket.lua)
    # ------------------------------------------------------------------
 
    def _token_bucket(self, keys: list[str], args: list[Any]) -> list[Any]:
        key         = keys[0]
        capacity    = float(args[0])
        refill_rate = float(args[1])
        requested   = float(args[2])
        now_ms      = float(args[3])
 
        bucket      = self._token_bucket_cache.get(key, {})
        tokens      = float(bucket.get("tokens", capacity))
        last_refill = float(bucket.get("last_refill_ms", now_ms))
 
        elapsed_sec = max(0.0, (now_ms - last_refill) / 1000.0)
        new_tokens  = min(capacity, tokens + elapsed_sec * refill_rate)
 
        allowed     = 0
        retry_after = 0
 
        if new_tokens >= requested:
            new_tokens -= requested
            allowed     = 1
        else:
            deficit     = requested - new_tokens
            retry_after = math.ceil((deficit / refill_rate) * 1000)
 
        self._token_bucket_cache[key] = {"tokens": new_tokens, "last_refill_ms": now_ms}
 
        remaining   = math.floor(new_tokens)
        reset_after = math.ceil(((capacity - new_tokens) / refill_rate) * 1000)
        return [allowed, remaining, reset_after, retry_after]

    # ------------------------------------------------------------------
    # Sliding window log (mirrors sliding_window_log.lua)
    # ------------------------------------------------------------------
 
    def _sliding_window_log(self, keys: list[str], args: list[Any]) -> list[Any]:
        key       = keys[0]
        limit     = int(args[0])
        window_ms = float(args[1])
        now_ms    = float(args[2])
 
        cutoff = now_ms - window_ms
 
        log: list[tuple[float, str]] = self._sliding_log_cache.get(key, [])
        log = [(s, m) for s, m in log if s > cutoff]  # prune expired
 
        count       = len(log)
        allowed     = 0
        retry_after = 0
 
        if count < limit:
            seq = self._sliding_log_seq_cache.get(key, 0) + 1
            self._sliding_log_seq_cache[key] = seq
            member = f"{now_ms}:{seq}"
            log.append((now_ms, member))
            allowed = 1
        else:
            if log:
                oldest      = log[0][0]
                retry_after = max(0, math.ceil(oldest + window_ms - now_ms))
 
        self._sliding_log_cache[key] = log
 
        remaining   = max(0, limit - count - (1 if allowed else 0))
        reset_after = int(window_ms)
        return [allowed, remaining, reset_after, retry_after]

    # ------------------------------------------------------------------
    # Sliding window counter (mirrors sliding_window_counter.lua)
    # ------------------------------------------------------------------
 
    def _sliding_window_counter(self, keys: list[str], args: list[Any]) -> list[Any]:
        prefix    = keys[0]
        limit     = float(args[0])
        window_ms = float(args[1])
        now_ms    = float(args[2])
 
        bucket_id = math.floor(now_ms / window_ms)
        cur_key   = f"{prefix}:{int(bucket_id)}"
        prev_key  = f"{prefix}:{int(bucket_id - 1)}"
 
        cur_count  = float(self._sliding_counter_cache.get(cur_key, 0))
        prev_count = float(self._sliding_counter_cache.get(prev_key, 0))
 
        position  = (now_ms % window_ms) / window_ms
        overlap   = 1.0 - position
        effective = prev_count * overlap + cur_count
 
        allowed     = 0
        retry_after = 0
 
        if effective < limit:
            self._sliding_counter_cache[cur_key] = cur_count + 1
            cur_count += 1
            allowed    = 1
        else:
            if prev_count > 0:
                target_overlap = (limit - cur_count) / prev_count
                if target_overlap < 0:
                    retry_after = math.ceil(window_ms - (now_ms % window_ms))
                else:
                    target_position = 1.0 - target_overlap
                    target_ms       = bucket_id * window_ms + target_position * window_ms
                    retry_after     = max(0, math.ceil(target_ms - now_ms))
            else:
                retry_after = math.ceil(window_ms - (now_ms % window_ms))
 
        effective_new = prev_count * overlap + cur_count
        remaining     = max(0, math.floor(limit - effective_new))
        reset_after   = math.ceil(window_ms - (now_ms % window_ms))
        return [allowed, remaining, reset_after, retry_after]


    # ------------------------------------------------------------------
    # Introspection (for metrics/benchmarks)
    # ------------------------------------------------------------------
 
    def estimate_memory_bytes(self, prefix: str = "rl:") -> int:
        """Rough byte estimate of keys matching prefix, across all caches."""
        total = 0
        for cache in (
            self._token_bucket_cache,
            self._sliding_log_cache,
            self._sliding_log_seq_cache,
            self._sliding_counter_cache,
        ):
            for k, v in list(cache.items()):
                if not k.startswith(prefix):
                    continue
                if isinstance(v, dict):
                    total += sum(len(str(x)) for x in v.items()) + 64
                elif isinstance(v, list):
                    total += len(v) * 64
                else:
                    total += 16
        return total
 
    def current_size(self) -> dict[str, int]:
        """Expose per-algorithm cache occupancy for metrics/observability."""
        return {
            "token_bucket": len(self._token_bucket_cache),
            "sliding_window_log": len(self._sliding_log_cache),
            "sliding_window_counter": len(self._sliding_counter_cache),
        }
 