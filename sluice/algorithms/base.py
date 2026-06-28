"""
Base types shared by all algorithms.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class RateLimitResult:
    """
    Returned by every limiter.check().

    Fields
    ------
    allowed         : bool  - whether the request should proceed
    remaining       : int   - estimated remaining quota in current window/bucket
    reset_after_ms  : int   - ms until quota fully resets (algorithm-specific meaning)
    retry_after_ms  : int   - ms to wait before retrying a rejected request (0 if allowed)
    algorithm       : str   - which algorithm produced this result
    key             : str   - the effective Redis key that was evaluated
    """

    allowed: bool
    remaining: int
    reset_after_ms: int
    retry_after_ms: int
    algorithm: str
    key: str


class RateLimiter(Protocol):
    """
    Protocol every algorithm must satisfy.
    Implementations are fully async; call `await backend.connect()` before use.
    """

    algorithm: str

    async def check(
        self,
        identifier: str,
        cost: int = 1,
    ) -> RateLimitResult:
        """
        Evaluate the rate limit for *identifier*.

        Parameters
        ----------
        identifier
            Opaque string that scopes the counter — typically ``f"{route}:{client_ip}"``.
        cost
            How many tokens / requests to consume (default 1).
            Token bucket supports cost > 1 natively; window-based algorithms
            treat cost > 1 as *cost* sequential single-unit requests and
            only allow them all if quota permits.
        """
        ...
