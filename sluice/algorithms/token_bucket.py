"""
Token Bucket rate limiter.

Characteristics
---------------
* Allows **bursting** up to `capacity` tokens.
* Refills at a constant `refill_rate` (tokens/sec).
* Memory: O(1) per key — two fields (tokens, last_refill_ms) in a Redis hash.
* Clock-skew effect: benign. A node with a fast clock refills slightly more;
  a node with a slow clock refills slightly less. In case, time is fetched from 
  the Redis instance; the error goes away at the cost of one extra round-trip.

Comparison edge
---------------
Unlike fixed-window counters, the token bucket never has a "reset spike" at
window boundaries. Unlike the sliding window log it costs O(1) memory regardless
of request volume.
"""

from __future__ import annotations

from dataclasses import dataclass

from sluice.backends.redis_backend import RedisBackend
from sluice.algorithms.base import RateLimitResult

ALGORITHM = "token_bucket"


@dataclass
class TokenBucket:
    """
    Parameters
    ----------
    backend      : RedisBackend
    capacity     : int   - max tokens (= max burst size)
    refill_rate  : float - tokens added per second
    key_prefix   : str   - Redis key namespace (default "rl:tb")
    """

    backend: RedisBackend
    capacity: int
    refill_rate: float
    key_prefix: str = "rl:tb"

    algorithm: str = ALGORITHM

    async def check(
        self,
        identifier: str,
        cost: int = 1,
    ) -> RateLimitResult:
        key = f"{self.key_prefix}:{identifier}"
        now = await self.backend.now_ms()

        result = await self.backend.evalsha(
            "token_bucket",
            keys=[key],
            args=[
                self.capacity,
                self.refill_rate,
                cost,
                now,
            ],
        )

        allowed, remaining, reset_after_ms, retry_after_ms = (
            bool(int(result[0])),
            int(result[1]),
            int(result[2]),
            int(result[3]),
        )

        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            reset_after_ms=reset_after_ms,
            retry_after_ms=retry_after_ms,
            algorithm=ALGORITHM,
            key=key,
        )