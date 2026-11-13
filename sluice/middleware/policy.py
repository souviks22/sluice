"""
Policy: declarative rate limit configuration.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping
from types import MappingProxyType
from hashlib import sha256

from fastapi import HTTPException, Request, Response

from sluice.backends import RateLimitBackend
from sluice.algorithms import RateLimiter, TokenBucket, SlidingWindowLog, SlidingWindowCounter

from sluice.middleware.utils import (
    LimitResolver, 
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
        # Give each rule an isolated namespace without mutating limiter objects
        # later during resolver construction. Explicit key_prefix wins.
        kwargs.setdefault("key_prefix", self._route_prefix(path, algorithm))
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
        """Return a resolver over a frozen snapshot of this policy.

        Route templates (for example ``/users/{user_id}``) are matched using
        Starlette's resolved route metadata, not the concrete request URL.
        """
        routes = self._snapshot()
        identifier_fn = self._identifier_fn

        def resolve(request: Request) -> tuple[str, RateLimiter]:
            route = request.scope.get("route")
            route_path = getattr(route, "path", request.url.path)
            limiter = routes.get(route_path, routes["*"])
            return identifier_fn(request), limiter

        return resolve

    def dependency(self, *, fail_open: bool = False) -> Callable[..., Any]:
        """Create a FastAPI dependency that enforces this policy per route.

        Attach it with ``dependencies=[Depends(policy.dependency())]`` on an
        ``APIRouter`` or application. The policy is snapshotted when the
        dependency is created. Backend errors fail closed with HTTP 503 by
        default; ``fail_open=True`` opts into availability over enforcement.
        """
        routes = self._snapshot()
        identifier_fn = self._identifier_fn

        async def enforce(request: Request, response: Response) -> None:
            route = request.scope.get("route")
            route_path = getattr(route, "path", request.url.path)
            limiter = routes.get(route_path, routes["*"])
            identifier = identifier_fn(request)
            try:
                result = await limiter.check(identifier)
            except Exception as exc:
                if fail_open:
                    return
                raise HTTPException(
                    status_code=503,
                    detail="Rate limit service unavailable",
                    headers={"Retry-After": "1"},
                ) from exc

            limit_value = getattr(limiter, "capacity", None)
            if limit_value is None:
                limit_value = getattr(limiter, "limit", None)
            response.headers["RateLimit-Limit"] = str(limit_value or "?")
            response.headers["RateLimit-Remaining"] = str(result.remaining)
            response.headers["RateLimit-Reset"] = str(max(1, (result.reset_after_ms + 999) // 1000))
            response.headers["X-RateLimit-Algorithm"] = result.algorithm
            if not result.allowed:
                retry = max(1, (result.retry_after_ms + 999) // 1000)
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "rate_limit_exceeded",
                        "algorithm": result.algorithm,
                        "retry_after_ms": result.retry_after_ms,
                    },
                    headers={
                        "Retry-After": str(retry),
                        "RateLimit-Limit": str(limit_value or "?"),
                        "RateLimit-Remaining": str(result.remaining),
                        "RateLimit-Reset": str(max(1, (result.reset_after_ms + 999) // 1000)),
                    },
                )

        enforce.__name__ = "sluice_rate_limit_dependency"
        return enforce

    def _snapshot(self) -> Mapping[str, RateLimiter]:
        if "*" not in self._routes:
            raise ValueError("Policy has no default rule. Call .default() first.")
        return MappingProxyType(dict(self._routes))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _route_prefix(path: str, algorithm: type[Any]) -> str:
        base = {
            TokenBucket: "rl:tb",
            SlidingWindowLog: "rl:swl",
            SlidingWindowCounter: "rl:swc",
        }.get(algorithm, "rl")
        if path == "*":
            suffix = "global"
        else:
            readable = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
            readable = readable.replace("-", "_") or "root"
            digest = sha256(path.encode("utf-8")).hexdigest()[:10]
            suffix = f"{readable[:40]}_{digest}"
        return f"{base}:{suffix}"

    def _build(
        self,
        algorithm: type[Any],
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
