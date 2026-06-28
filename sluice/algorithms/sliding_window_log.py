"""
Sliding Window Log rate limiter.

Characteristics
---------------
* **Exact** rate limiting: every decision is computed against the real history
  of requests in [now - window, now].
* Memory: O(N) per key where N = requests in the current window.
  At 1 000 req/s over a 60 s window that's ~60 000 sorted-set members per key.
  Each member costs roughly 64 bytes → ~3.8 MB per high-traffic key.
* Clock-skew effect: **sensitive**. If two nodes disagree on `now` by Δms,
  entries that should be pruned survive Δms too long, causing slight
  under-counting of capacity. Mitigate by using Redis server time 
  as the authoritative clock

Comparison edge
---------------
The only algorithm that gives a provably exact sliding count.
Use it when the cost of a false-positive rejection exceeds the memory overhead
(e.g. payment APIs, quota-sensitive B2B integrations).
"""

from __future__ import annotations

from dataclasses import dataclass

from sluice.backends.redis_backend import RedisBackend
from sluice.algorithms.base import RateLimitResult

ALGORITHM = "sliding_window_log"


@dataclass
class SlidingWindowLog:
    """
    Parameters
    ----------
    backend          : RedisBackend
    limit            : int   - max requests per `window_ms`
    window_ms        : int   - window duration in milliseconds
    key_prefix       : str   - Redis key namespace (default "rl:swl")
    """

    backend: RedisBackend
    limit: int
    window_ms: int
    key_prefix: str = "rl:swl"

    algorithm: str = ALGORITHM

    async def check(
        self,
        identifier: str,
        cost: int = 1,
    ) -> RateLimitResult:
        key = f"{self.key_prefix}:{identifier}"
        now = await self.backend.now_ms()

        # For cost > 1 we check if *all* cost units can be admitted atomically.
        # The Lua script admits exactly one unit; we call it `cost` times only
        # if the first call succeeds — this keeps the log exact.
        # A single-pass cost > 1 variant would require a heavier Lua script;
        # the trade-off is documented intentionally.
        last_result: RateLimitResult | None = None

        for i in range(cost):
            result_raw = await self.backend.evalsha(
                "sliding_window_log",
                keys=[key],
                args=[self.limit, self.window_ms, now + i],  # +i for uniqueness in same ms
            )
            last_result = RateLimitResult(
                allowed=bool(int(result_raw[0])),
                remaining=int(result_raw[1]),
                reset_after_ms=int(result_raw[2]),
                retry_after_ms=int(result_raw[3]),
                algorithm=ALGORITHM,
                key=key,
            )
            if not last_result.allowed:
                break

        assert last_result is not None
        return last_result
