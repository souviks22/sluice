from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from sluice.algorithms import SlidingWindowLog, TokenBucket
from sluice.domain import RateLimitResult
from sluice.middleware import RateLimitPolicy


class StubBackend:
    def __init__(self, allowed: bool = True, fail: bool = False):
        self.allowed = allowed
        self.fail = fail
        self.calls = []

    async def check(self, operation):
        self.calls.append(operation)
        if self.fail:
            raise RuntimeError("backend unavailable")
        return RateLimitResult(
            allowed=self.allowed,
            remaining=0 if not self.allowed else 4,
            reset_after_ms=10_000,
            retry_after_ms=2_000 if not self.allowed else 0,
            algorithm=operation.algorithm,
            key=operation.key,
        )


def test_dependency_matches_route_template_and_emits_headers():
    backend = StubBackend()
    policy = (
        RateLimitPolicy(backend)
        .limit("/users/{user_id}", SlidingWindowLog, limit=5, window="1m")
        .default(TokenBucket, capacity=10, window="1m")
    )
    dependency = policy.dependency()
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(dependency)])

    @router.get("/users/{user_id}")
    async def user(user_id: str):
        return {"user_id": user_id}

    app.include_router(router)
    response = TestClient(app).get("/users/abc")
    assert response.status_code == 200
    assert response.headers["RateLimit-Limit"] == "5"
    assert backend.calls[0].algorithm == "sliding_window_log"
    assert backend.calls[0].key.startswith("rl:swl:users_user_id_")


def test_dependency_rejects_with_retry_after():
    backend = StubBackend(allowed=False)
    policy = RateLimitPolicy(backend).default(TokenBucket, capacity=10, window="1m")
    app = FastAPI()

    @app.get("/", dependencies=[Depends(policy.dependency())])
    async def root():
        return {"ok": True}

    response = TestClient(app).get("/")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2"
    assert response.headers["RateLimit-Remaining"] == "0"


def test_dependency_fails_closed_with_service_unavailable():
    backend = StubBackend(fail=True)
    policy = RateLimitPolicy(backend).default(TokenBucket, capacity=10, window="1m")
    app = FastAPI()

    @app.get("/", dependencies=[Depends(policy.dependency())])
    async def root():
        return {"ok": True}

    assert TestClient(app).get("/").status_code == 503


def test_resolver_creation_does_not_mutate_limiter_prefix():
    backend = StubBackend()
    policy = (
        RateLimitPolicy(backend)
        .limit("/a", SlidingWindowLog, limit=5, window="1m")
        .default(TokenBucket, capacity=10, window="1m")
    )
    before = policy._routes["/a"].key_prefix
    policy.resolver()
    policy.resolver()
    assert policy._routes["/a"].key_prefix == before
