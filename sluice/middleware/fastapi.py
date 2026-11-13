"""Optional pure-ASGI rate-limit middleware.

FastAPI route dependencies are the preferred integration. This middleware
remains useful for app-wide policies and non-route-specific enforcement.
"""

from __future__ import annotations

import inspect
import logging
from math import ceil
from typing import Callable, Literal

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sluice.algorithms import RateLimitResult, RateLimiter
from sluice.middleware.utils import LimitResolver, ip_identifier

logger = logging.getLogger(__name__)

FailureMode = Literal["closed", "open"]


class RateLimitMiddleware:
    """Enforce a resolver-selected limiter at the ASGI boundary.

    ``exclude_paths`` entries match exactly and, for non-root paths, their
    descendants (``/health`` matches ``/health/live`` but not
    ``/healthcheck``). Backend errors fail closed with 503 by default.
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
        *,
        failure_mode: FailureMode = "closed",
        fail_open: bool | None = None,
        legacy_headers: bool = True,
    ) -> None:
        if resolver is not None:
            self._resolver = resolver
        elif limiter is not None:
            self._resolver = lambda request: (identifier_fn(request), limiter)
        else:
            raise ValueError("RateLimitMiddleware requires either 'resolver' or 'limiter'.")

        if fail_open is not None:
            # Compatibility shim for callers of the pre-Step-9 API.
            failure_mode = "open" if fail_open else "closed"
        if failure_mode not in ("open", "closed"):
            raise ValueError("failure_mode must be 'open' or 'closed'.")

        self.app = app
        self.exclude_paths = frozenset(
            p.rstrip("/") or "/" for p in (
                exclude_paths if exclude_paths is not None
                else {"/health", "/metrics", "/docs", "/openapi.json"}
            )
        )
        self.soft_mode = soft_mode
        self.failure_mode = failure_mode
        self.legacy_headers = legacy_headers
        self._on_limit_exceeded = on_limit_exceeded or self._default_rejection

    @staticmethod
    def _default_rejection(request: Request, result: RateLimitResult) -> Response:
        retry_seconds = max(1, ceil(result.retry_after_ms / 1000))
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "algorithm": result.algorithm,
                "retry_after_ms": result.retry_after_ms,
            },
            headers={"Retry-After": str(retry_seconds)},
        )

    def _should_skip(self, path: str) -> bool:
        for excluded in self.exclude_paths:
            if path == excluded:
                return True
            if excluded != "/" and path.startswith(excluded + "/"):
                return True
        return False

    @staticmethod
    def _limit_value(limiter: RateLimiter) -> str:
        value = getattr(limiter, "capacity", None)
        if value is None:
            value = getattr(limiter, "limit", None)
        return str(value if value is not None else "?")

    def _headers(self, limiter: RateLimiter, result: RateLimitResult) -> list[tuple[bytes, bytes]]:
        headers = [
            (b"ratelimit-limit", self._limit_value(limiter).encode("ascii")),
            (b"ratelimit-remaining", str(result.remaining).encode("ascii")),
            (b"ratelimit-reset", str(max(1, ceil(result.reset_after_ms / 1000))).encode("ascii")),
        ]
        if self.legacy_headers:
            headers.append((b"x-ratelimit-algorithm", result.algorithm.encode("ascii", "replace")))
        if not result.allowed:
            retry = max(1, ceil(result.retry_after_ms / 1000))
            headers.append((b"retry-after", str(retry).encode("ascii")))
        return headers

    @staticmethod
    def _merge_headers(existing: list[tuple[bytes, bytes]], additions: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        replaced = {name for name, _ in additions}
        return [(name, value) for name, value in existing if name.lower() not in replaced] + additions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        if self._should_skip(path):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        try:
            resolved = self._resolver(request)
            if inspect.isawaitable(resolved):
                resolved = await resolved
            identifier, limiter = resolved
            result = await limiter.check(identifier)
        except Exception:
            logger.exception("Rate-limit evaluation failed")
            if self.failure_mode == "open":
                await self.app(scope, receive, send)
                return
            response: Response = JSONResponse(
                status_code=503,
                content={"error": "rate_limit_service_unavailable"},
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return

        headers = self._headers(limiter, result)
        if not result.allowed and not self.soft_mode:
            response = self._on_limit_exceeded(request, result)
            if inspect.isawaitable(response):
                response = await response
            response.raw_headers = self._merge_headers(response.raw_headers, headers)
            await response(scope, receive, send)
            return

        async def send_with_rate_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = self._merge_headers(list(message.get("headers", [])), headers)
                if not result.allowed and self.soft_mode:
                    message["headers"] = self._merge_headers(
                        message["headers"], [(b"x-ratelimit-exceeded", b"true")]
                    )
            await send(message)

        await self.app(scope, receive, send_with_rate_headers)
