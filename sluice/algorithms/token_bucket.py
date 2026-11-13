"""
Token Bucket rate limiter.

Characteristics
---------------
* Allows **bursting** up to `capacity` tokens.
* Refills at a constant `refill_rate` (tokens/sec).
* Memory: O(1) per key — two fields (tokens, last_refill_ms) in a Redis hash.
* Clock-skew effect: application-node skew is eliminated for Redis-backed
  execution because the Lua transition reads Redis TIME atomically with state.

Comparison edge
---------------
Unlike fixed-window counters, the token bucket never has a "reset spike" at
window boundaries. Unlike the sliding window log it costs O(1) memory regardless
of request volume.
"""

from __future__ import annotations

from dataclasses import dataclass

from sluice.backends import RateLimitBackend
from sluice.algorithms.base import RateLimitResult
from sluice.domain import RateLimitOperation, TokenBucketConfig
from sluice.domain.validation import (
    positive_quota,
    validate_cost,
    validate_identifier,
    validate_key_prefix,
    validate_refill_rate,
)

ALGORITHM = "token_bucket"


@dataclass
class TokenBucket:
    """
    Parameters
    ----------
    backend      : RateLimitBackend
    capacity     : int   - max tokens (= max burst size)
    refill_rate  : float - tokens added per second
    key_prefix   : str   - Redis key namespace (default "rl:tb")
    """

    backend: RateLimitBackend
    capacity: int
    refill_rate: float
    key_prefix: str = "rl:tb"

    algorithm: str = ALGORITHM

    def __post_init__(self) -> None:
        positive_quota(self.capacity, "capacity")
        self.refill_rate = validate_refill_rate(self.capacity, self.refill_rate)
        validate_key_prefix(self.key_prefix)

    async def check(
        self,
        identifier: str,
        cost: int = 1,
    ) -> RateLimitResult:
        validate_identifier(identifier)
        validate_cost(cost)
        key = f"{self.key_prefix}:{identifier}"
        result = await self.backend.check(RateLimitOperation(
            algorithm=ALGORITHM,
            key=key,
            config=TokenBucketConfig(key_prefix=self.key_prefix, capacity=self.capacity, refill_rate=self.refill_rate),
            cost=cost,
        ))
        return result

