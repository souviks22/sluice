"""
Sliding Window Counter rate limiter.

Characteristics
---------------
* **Approximate** sliding window via weighted interpolation of two fixed buckets.
* Memory: O(1) per key — only two integer counters regardless of request volume.
* The approximation error is bounded: in the worst case the effective count
  can be off by at most `limit * (1 - overlap)` requests at the bucket boundary.
  In practice the error averages < 5% of the limit.
* Clock-skew effect: **moderate**. Skew shifts which bucket a node writes to.
  If two nodes diverge by more than `window_ms`, they write to different buckets,
  creating phantom capacity. Recommended: keep NTP sync within 100ms.

Memory comparison (concrete numbers at 1 000 req/min limit)
-------------------------------------------------------------
  Token bucket log     : 2 fields * 8 bytes ≈ 16 bytes / key
  Sliding window counter: 2 counters * 8 bytes ≈ 16 bytes / key   ← this
  Sliding window log    : up to 1 000 members * ~64 bytes ≈ 64 KB / key

Comparison edge
---------------
Best memory-to-accuracy trade-off at high traffic volumes. Preferred default
for most web APIs where a ±5% error on the rate limit is acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass

from sluice.backends.base import RateLimitBackend
from sluice.algorithms.base import RateLimitResult

ALGORITHM = "sliding_window_counter"


@dataclass
class SlidingWindowCounter:
    """
    Parameters
    ----------
    backend      : RateLimitBackend
    limit        : int - max (approximate) requests per `window_ms`
    window_ms    : int - window duration in milliseconds
    key_prefix   : str - Redis key namespace (default "rl:swc")
    """

    backend: RateLimitBackend
    limit: int
    window_ms: int
    key_prefix: str = "rl:swc"

    algorithm: str = ALGORITHM

    async def check(
        self,
        identifier: str,
        cost: int = 1,
    ) -> RateLimitResult:
        key = f"{self.key_prefix}:{identifier}"
        now = await self.backend.now_ms()

        last_result: RateLimitResult | None = None

        for _ in range(cost):
            result_raw = await self.backend.evalsha(
                "sliding_window_counter",
                keys=[key],
                args=[self.limit, self.window_ms, now],
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
