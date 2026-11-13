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

from sluice.backends import RateLimitBackend
from sluice.algorithms.base import RateLimitResult
from sluice.domain import RateLimitOperation, SlidingWindowConfig
from sluice.domain.validation import (
    positive_quota,
    positive_window_ms,
    validate_cost,
    validate_identifier,
    validate_key_prefix,
)

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

    def __post_init__(self) -> None:
        positive_quota(self.limit, "limit")
        positive_window_ms(self.window_ms)
        validate_key_prefix(self.key_prefix)

    async def check(
        self,
        identifier: str,
        cost: int = 1,
    ) -> RateLimitResult:
        validate_identifier(identifier)
        validate_cost(cost)
        key = f"{self.key_prefix}:{identifier}"
        return await self.backend.check(RateLimitOperation(
            algorithm=ALGORITHM,
            key=key,
            config=SlidingWindowConfig(key_prefix=self.key_prefix, limit=self.limit, window_ms=self.window_ms),
            cost=cost,
        ))
