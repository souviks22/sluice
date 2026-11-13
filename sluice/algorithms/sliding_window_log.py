"""
Sliding Window Log rate limiter.

Characteristics
---------------
* **Exact** rate limiting: every decision is computed against the real history
  of requests in (now - window, now].
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

from sluice.backends.base import RateLimitBackend
from sluice.algorithms.base import RateLimitResult
from sluice.domain import RateLimitOperation, SlidingWindowConfig
from sluice.domain.validation import (
    positive_quota,
    positive_window_ms,
    validate_cost,
    validate_identifier,
    validate_key_prefix,
)

ALGORITHM = "sliding_window_log"


@dataclass
class SlidingWindowLog:
    """
    Parameters
    ----------
    backend          : RateLimitBackend
    limit            : int   - max requests per `window_ms`
    window_ms        : int   - window duration in milliseconds
    key_prefix       : str   - Redis key namespace (default "rl:swl")
    """

    backend: RateLimitBackend
    limit: int
    window_ms: int
    key_prefix: str = "rl:swl"

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
