"""
FastAPI middleware for distributed rate limiting.

Design choices
--------------
* **Zero decorator overhead**: rate limiting is enforced at the ASGI layer,
  before the request even reaches a router. This means *every* route is
  protected by default; per-route overrides use the `RateLimitDependency`.
* **Identifier strategy** is pluggable — swap from IP-based to JWT-subject
  or composite keys without touching algorithm code.
* **Response headers** follow the IETF draft-ietf-httpapi-ratelimit-headers
  spec (RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset, Retry-After).
* **Asymmetric rejection**: the middleware can be configured to *tag* requests
  rather than reject them (soft mode), letting downstream handlers decide.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from sluice.algorithms.base import RateLimitResult, RateLimiter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Identifier extractors
# ---------------------------------------------------------------------------

def ip_identifier(request: Request) -> str:
    """Use the client IP address (respects X-Forwarded-For if behind a proxy)."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def ip_route_identifier(request: Request) -> str:
    """Scope per (IP, route path) — different limits per endpoint."""
    ip = ip_identifier(request)
    path = request.url.path
    return f"{ip}:{path}"


def jwt_subject_identifier(request: Request) -> str:
    """
    Extract 'sub' from a Bearer JWT without verification.
    Falls back to IP if no Authorization header is present.
    Verification should happen in your auth middleware, not here.
    """
    import base64
    import json

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        try:
            payload_b64 = token.split(".")[1]
            # Pad base64url
            padding = 4 - len(payload_b64) % 4
            payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
            return payload.get("sub", ip_identifier(request))
        except Exception:
            pass
    return ip_identifier(request)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that gates every request through a RateLimiter.

    Parameters
    ----------
    app             : ASGI application
    limiter         : any object satisfying the RateLimiter protocol
    identifier_fn   : callable (Request) -> str  (default: ip_identifier)
    exclude_paths   : set of path prefixes to skip (e.g. {"/health", "/metrics"})
    soft_mode       : if True, rejected requests are *tagged* (X-RateLimit-Exceeded: true)
                      but not blocked — useful for shadow-testing a new policy
    on_limit_exceeded : optional callable for custom 429 responses
    """

    def __init__(
        self,
        app: ASGIApp,
        limiter: RateLimiter,
        identifier_fn: Callable[[Request], str] = ip_identifier,
        exclude_paths: set[str] | None = None,
        soft_mode: bool = False,
        on_limit_exceeded: Callable[[Request, RateLimitResult], Response] | None = None,
    ) -> None:
        super().__init__(app)
        self.limiter = limiter
        self.identifier_fn = identifier_fn
        self.exclude_paths = exclude_paths or {"/health", "/metrics", "/docs", "/openapi.json"}
        self.soft_mode = soft_mode
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
            headers={
                "Retry-After": str(max(1, result.retry_after_ms // 1000)),
            },
        )

    def _should_skip(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.exclude_paths)

    async def dispatch(
        self, 
        request: Request, 
        call_next: Callable[[Request], Coroutine[Any, Any, Response]],
    ) -> Response:
        if self._should_skip(request.url.path):
            return await call_next(request)

        identifier = self.identifier_fn(request)
        try:
            result = await self.limiter.check(identifier)
        except Exception as exc:
            # Never let rate-limiter failures take down the service
            logger.warning("Rate limiter error (failing open): %s", exc)
            return await call_next(request)

        # Attach standard rate-limit headers to every response
        def _add_headers(response: Response) -> Response:
            response.headers["RateLimit-Limit"]     = str(
                getattr(self.limiter, "capacity", None)
                or getattr(self.limiter, "limit", "?")
            )
            response.headers["RateLimit-Remaining"] = str(result.remaining)
            response.headers["RateLimit-Reset"]     = str(max(1, result.reset_after_ms // 1000))
            response.headers["X-RateLimit-Algorithm"] = result.algorithm
            return response

        if result.allowed:
            response = await call_next(request)
            return _add_headers(response)

        # Soft mode: let the request through but tag it
        if self.soft_mode:
            response = await call_next(request)
            response.headers["X-RateLimit-Exceeded"] = "true"
            response.headers["Retry-After"]          = str(max(1, result.retry_after_ms // 1000))
            return _add_headers(response)

        # Hard rejection
        return self._on_limit_exceeded(request, result)


# ---------------------------------------------------------------------------
# Per-route dependency (override middleware policy for specific endpoints)
# ---------------------------------------------------------------------------

class RateLimitDependency:
    """
    FastAPI dependency for per-route rate limiting with a *different* limiter
    than the global middleware.

    Example::

        strict_limiter = TokenBucket(backend, capacity=5, refill_rate=1.0)
        check_strict = RateLimitDependency(strict_limiter)

        @app.post("/auth/login")
        async def login(request: Request, _: None = Depends(check_strict)):
            ...
    """

    def __init__(
        self,
        limiter: RateLimiter,
        identifier_fn: Callable[[Request], str] = ip_identifier,
        cost: int = 1,
    ) -> None:
        self.limiter = limiter
        self.identifier_fn = identifier_fn
        self.cost = cost

    async def __call__(self, request: Request) -> RateLimitResult:
        identifier = self.identifier_fn(request)
        result = await self.limiter.check(identifier, cost=self.cost)
        if not result.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "algorithm": result.algorithm,
                    "retry_after_ms": result.retry_after_ms,
                },
                headers={"Retry-After": str(max(1, result.retry_after_ms // 1000))},
            )
        return result
