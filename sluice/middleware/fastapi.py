"""
FastAPI middleware for distributed rate limiting.

Primary API: resolver pattern
------------------------------
A resolver is a callable (Request) -> (identifier, RateLimiter).
The middleware calls it on every request to determine both *who* is making
the request and *which* limit applies.
"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from sluice.algorithms import RateLimitResult, RateLimiter
from sluice.middleware.utils import LimitResolver, ip_identifier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware — single enforcement point for all rate limiting.

    Parameters
    ----------
    app               : ASGI application
    resolver          : LimitResolver — primary API.
                        callable (Request) -> (identifier: str, limiter: RateLimiter)
    limiter           : RateLimiter — legacy API. Used when resolver is not provided.
    identifier_fn     : callable (Request) -> str — legacy API, paired with limiter.
    exclude_paths     : path prefixes to skip entirely (e.g. "/health", "/docs")
    soft_mode         : tag over-limit requests instead of rejecting them
    on_limit_exceeded : optional callable for custom 429 responses
    """

    def __init__(
        self,
        app: ASGIApp,
        resolver: LimitResolver | None = None,
        limiter: RateLimiter | None = None,
        identifier_fn: Callable[[Request], str] = ip_identifier,
        exclude_paths: set[str] | None = None,
        soft_mode: bool = False,
        on_limit_exceeded: Callable[[Request, RateLimitResult], Response] | None = None,
    ) -> None:
        super().__init__(app)

        if resolver is not None:
            self._resolver = resolver
        elif limiter is not None:
            _limiter = limiter
            _id_fn   = identifier_fn
            self._resolver = lambda req: (_id_fn(req), _limiter)
        else:
            raise ValueError("RateLimitMiddleware requires either 'resolver' or 'limiter'.")

        self.exclude_paths     = exclude_paths or {"/health", "/metrics", "/docs", "/openapi.json"}
        self.soft_mode         = soft_mode
        self._on_limit_exceeded = on_limit_exceeded or self._default_rejection

    @staticmethod
    def _default_rejection(request: Request, result: RateLimitResult) -> Response:
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "algorithm": result.algorithm,
                "retry_after_ms": result.retry_after_ms,
            },
            headers={"Retry-After": str(max(1, result.retry_after_ms // 1000))},
        )

    def _should_skip(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.exclude_paths)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if self._should_skip(request.url.path):
            return await call_next(request)

        try:
            identifier, limiter = self._resolver(request)
            result = await limiter.check(identifier)
        except Exception as exc:
            logger.warning("Rate limiter error (failing open): %s", exc)
            return await call_next(request)

        # Read the limit from the resolved limiter — correct when different
        # routes use different limiters with different limits.
        limit_value = str(
            getattr(limiter, "capacity", None)
            or getattr(limiter, "limit", "?")
        )

        def _attach_headers(response: Response) -> Response:
            response.headers["RateLimit-Limit"]       = limit_value
            response.headers["RateLimit-Remaining"]   = str(result.remaining)
            response.headers["RateLimit-Reset"]       = str(max(1, result.reset_after_ms // 1000))
            response.headers["X-RateLimit-Algorithm"] = result.algorithm
            return response

        if result.allowed:
            response = await call_next(request)
            return _attach_headers(response)

        if self.soft_mode:
            response = await call_next(request)
            response.headers["X-RateLimit-Exceeded"] = "true"
            response.headers["Retry-After"] = str(max(1, result.retry_after_ms // 1000))
            return _attach_headers(response)

        return self._on_limit_exceeded(request, result)
