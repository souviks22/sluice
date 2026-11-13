"""
Base types shared by all algorithms.
"""

from __future__ import annotations

from typing import Protocol

from sluice.backends.base import RateLimitBackend
from sluice.domain import RateLimitResult


class RateLimiter(Protocol):
    """
    Protocol every algorithm must satisfy.
    Implementations are fully async; call `await backend.connect()` before use.
    """

    backend: RateLimitBackend
    key_prefix: str = "rl"
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
            Positive integer quota units to consume (default 1). Admission
            must be all-or-nothing for weighted requests.
        """
        ...
