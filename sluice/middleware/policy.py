"""
Policy: declarative rate limit configuration.
"""

from __future__ import annotations

from typing import Any, Callable

from sluice.backends import RateLimitBackend
from sluice.algorithms import RateLimiter, TokenBucket, SlidingWindowLog, SlidingWindowCounter

from sluice.middleware.utils import (
    LimitResolver, 
    build_resolver, 
    ip_route_identifier,
    parse_rate,
    parse_window,
)


# ---------------------------------------------------------------------------
# RateLimitPolicy
# ---------------------------------------------------------------------------

class RateLimitPolicy:
    """
    Declarative rate limit policy builder.

    Usage::
        policy = (
            RateLimitPolicy(backend)
            .limit("/api/login",  SlidingWindowLog,  limit=5,   window="1m")
            .limit("/api/upload", TokenBucket, capacity=10, window="1m")
            .default(TokenBucket, capacity=100, window="1m")
        )

        app.add_middleware(RateLimitMiddleware, resolver=policy.resolver())
    """

    def __init__(self, backend: RateLimitBackend) -> None:
        self._backend = backend
        self._routes: dict[str, RateLimiter] = {}
        self._identifier_fn = ip_route_identifier

    # ------------------------------------------------------------------
    # Fluent API
    # ------------------------------------------------------------------

    def limit(
        self,
        path: str,
        algorithm: type,
        *,
        # SlidingWindowLog / SlidingWindowCounter
        limit: int | None = None,
        # TokenBucket
        capacity: int | None = None,
        rate: str | None = None,
        # shared
        window: str | None = None,
        **kwargs: Any,
    ) -> "RateLimitPolicy":
        """
        Add a rate limit rule for a specific path.

        Parameters
        ----------
        path      : route path, e.g. "/api/login"
        algorithm : TokenBucket, SlidingWindowLog, or SlidingWindowCounter
        limit     : max requests per window (SWLog / SWCounter)
        capacity  : max burst tokens (TokenBucket)
        rate      : refill rate string, e.g. "100/min" (TokenBucket)
        window    : window size string, e.g. "1m", "30s"
        **kwargs  : passed through to the algorithm constructor (e.g. use_server_time=True)
        """
        limiter = self._build(algorithm, limit=limit, capacity=capacity,
                                rate=rate, window=window, **kwargs)
        self._routes[path] = limiter
        return self

    def default(
        self,
        algorithm: type,
        *,
        limit: int | None = None,
        capacity: int | None = None,
        rate: str | None = None,
        window: str | None = None,
        **kwargs: Any,
    ) -> "RateLimitPolicy":
        """
        Set the catch-all rule (equivalent to route "*").
        Must be called before resolver() — Policy.resolver() raises if missing.
        """
        return self.limit("*", algorithm, limit=limit, capacity=capacity,
                          rate=rate, window=window, **kwargs)

    def identifier(self, fn: Callable) -> "RateLimitPolicy":
        """
        Override the identifier function (default: ip_route_identifier).

        Example::
            policy.identifier(ip_identifier)         # IP only
            policy.identifier(jwt_subject_identifier) # JWT sub
            policy.identifier(lambda r: r.headers["X-API-Key"])
        """
        self._identifier_fn = fn
        return self

    def resolver(self) -> LimitResolver:
        """
        Build and return the LimitResolver for use in RateLimitMiddleware.
        Raises ValueError if no default() has been set.
        """
        if "*" not in self._routes:
            raise ValueError(
                "Policy has no default rule. Call .default() before .resolver()."
            )
        return build_resolver(self._routes, identifier_fn=self._identifier_fn)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build(
        self,
        algorithm: type,
        *,
        limit: int | None,
        capacity: int | None,
        rate: str | None,
        window: str | None,
        **kwargs: Any,
    ) -> RateLimiter:
        window_ms = parse_window(window) if window else None

        if algorithm is TokenBucket:
            if capacity is None:
                raise ValueError("TokenBucket requires 'capacity'.")
            if window_ms is None and rate is None:
                raise ValueError("TokenBucket requires 'window' or 'rate'.")
            if rate is not None:
                refill = parse_rate(rate)
            elif window_ms is not None:
                # Derive rate from capacity/window: fill the bucket in one window
                refill = capacity / (window_ms / 1000)
            else:
                raise ValueError("TokenBucket requires 'window' or 'rate'.")
            return TokenBucket(self._backend, capacity=capacity, refill_rate=refill, **kwargs)

        elif algorithm in (SlidingWindowLog, SlidingWindowCounter):
            if limit is None:
                raise ValueError(f"{algorithm.__name__} requires 'limit'.")
            if window_ms is None:
                raise ValueError(f"{algorithm.__name__} requires 'window'.")
            return algorithm(self._backend, limit=limit, window_ms=window_ms, **kwargs)

        else:
            raise ValueError(
                f"Unknown algorithm class {algorithm}. "
                f"Use TokenBucket, SlidingWindowLog, or SlidingWindowCounter."
            )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def describe(self) -> list[dict[str, Any]]:
        """
        Return a human-readable summary of all configured routes.
        Useful for logging at startup.

        Example output::
            [
              {"route": "/api/login", "algorithm": "sliding_window_log", "limit": 5, "window_ms": 60000},
              {"route": "*",          "algorithm": "token_bucket",        "capacity": 100},
            ]
        """
        rows = []
        for route, limiter in self._routes.items():
            row: dict[str, Any] = {
                "route":     route,
                "algorithm": getattr(limiter, "algorithm", type(limiter).__name__),
            }
            for attr in ("limit", "capacity", "refill_rate", "window_ms", "key_prefix"):
                val = getattr(limiter, attr, None)
                if val is not None:
                    row[attr] = val
            rows.append(row)
        return rows
