from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from sluice.domain import RateLimitResult
from sluice.middleware.fastapi import RateLimitMiddleware


class StubLimiter:
    capacity = 3

    def __init__(self, result=None, error=None):
        self.result = result or RateLimitResult(True, 2, 1500, 0, "token_bucket", "test:key")
        self.error = error
        self.calls = 0

    async def check(self, identifier):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def make_app(limiter, **middleware_options):
    async def endpoint(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/{path:path}", endpoint)])
    app.add_middleware(RateLimitMiddleware, limiter=limiter, **middleware_options)
    return app


def test_success_headers_and_ceil_reset():
    client = TestClient(make_app(StubLimiter()))
    response = client.get("/resource")
    assert response.status_code == 200
    assert response.headers["RateLimit-Limit"] == "3"
    assert response.headers["RateLimit-Remaining"] == "2"
    assert response.headers["RateLimit-Reset"] == "2"
    assert response.headers["X-RateLimit-Algorithm"] == "token_bucket"


def test_rejection_contains_standard_headers_and_retry_after():
    result = RateLimitResult(False, 0, 1200, 1001, "token_bucket", "test:key")
    response = TestClient(make_app(StubLimiter(result))).get("/resource")
    assert response.status_code == 429
    assert response.headers["RateLimit-Remaining"] == "0"
    assert response.headers["Retry-After"] == "2"


def test_exclusions_are_segment_aware():
    limiter = StubLimiter()
    client = TestClient(make_app(limiter, exclude_paths={"/health"}))
    assert client.get("/health").status_code == 200
    assert client.get("/health/live").status_code == 200
    assert client.get("/healthcheck").status_code == 200
    assert limiter.calls == 1


def test_backend_failure_fails_closed_by_default():
    response = TestClient(make_app(StubLimiter(error=RuntimeError("down")))).get("/resource")
    assert response.status_code == 503
    assert response.json()["error"] == "rate_limit_service_unavailable"


def test_backend_failure_can_explicitly_fail_open():
    limiter = StubLimiter(error=RuntimeError("down"))
    response = TestClient(make_app(limiter, failure_mode="open")).get("/resource")
    assert response.status_code == 200


def test_legacy_fail_open_flag_remains_supported():
    limiter = StubLimiter(error=RuntimeError("down"))
    response = TestClient(make_app(limiter, fail_open=True)).get("/resource")
    assert response.status_code == 200


def test_custom_rejection_response_receives_rate_headers():
    result = RateLimitResult(False, 0, 1000, 500, "token_bucket", "test:key")
    limiter = StubLimiter(result)
    app = make_app(limiter, on_limit_exceeded=lambda request, outcome: JSONResponse({"custom": True}, status_code=429))
    response = TestClient(app).get("/resource")
    assert response.status_code == 429
    assert response.json() == {"custom": True}
    assert response.headers["RateLimit-Limit"] == "3"
