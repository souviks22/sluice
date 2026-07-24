"""
Tests for the RateLimitPolicy class and time/rate string parsers.
"""
from __future__ import annotations

import fakeredis
import pytest
import pytest_asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sluice import RateLimitPolicy
from sluice.algorithms import TokenBucket, SlidingWindowLog, SlidingWindowCounter
from sluice.middleware import parse_window, parse_rate

from sluice.backends import RedisBackend


@pytest_asyncio.fixture
async def backend():
    redis_client = fakeredis.FakeAsyncRedis()
    b = RedisBackend(client=redis_client)
    await b.connect()
    yield b
    await b.close()


# ---------------------------------------------------------------------------
# parse_window
# ---------------------------------------------------------------------------

class TestParseWindow:

    def test_minutes(self):
        assert parse_window("1m")  == 60_000
        assert parse_window("2m")  == 120_000
        assert parse_window("30m") == 1_800_000

    def test_seconds(self):
        assert parse_window("1s")  == 1_000
        assert parse_window("30s") == 30_000

    def test_hours(self):
        assert parse_window("1h")  == 3_600_000
        assert parse_window("2h")  == 7_200_000

    def test_milliseconds(self):
        assert parse_window("500ms") == 500
        assert parse_window("100ms") == 100

    def test_whitespace_ignored(self):
        assert parse_window("  1m  ") == 60_000

    def test_case_insensitive(self):
        assert parse_window("1M") == 60_000
        assert parse_window("1S") == 1_000

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Cannot parse window"):
            parse_window("1x")
        with pytest.raises(ValueError, match="Cannot parse window"):
            parse_window("one minute")


# ---------------------------------------------------------------------------
# parse_rate
# ---------------------------------------------------------------------------

class TestParseRate:

    def test_per_minute(self):
        assert abs(parse_rate("60/min") - 1.0)  < 1e-9
        assert abs(parse_rate("120/m")  - 2.0)  < 1e-9

    def test_per_second(self):
        assert abs(parse_rate("10/s")   - 10.0) < 1e-9

    def test_per_hour(self):
        assert abs(parse_rate("3600/h") - 1.0)  < 1e-9

    def test_plain_float(self):
        assert abs(parse_rate("1.5")    - 1.5)  < 1e-9

    def test_invalid_unit_raises(self):
        with pytest.raises(ValueError, match="Unknown rate unit"):
            parse_rate("10/week")

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Cannot parse rate"):
            parse_rate("fast")


# ---------------------------------------------------------------------------
# RateLimitPolicy fluent API
# ---------------------------------------------------------------------------

class TestRateLimitPolicyFluent:

    def test_basic_construction(self, backend):
        policy  = (
            RateLimitPolicy(backend)
            .limit("/api/login", SlidingWindowLog, limit=5, window="1m")
            .default(TokenBucket, capacity=100, window="1m")
        )
        resolver = policy.resolver()
        assert resolver is not None

    def test_resolver_raises_without_default(self, backend):
        policy  = RateLimitPolicy(backend).limit("/api/login", SlidingWindowLog, limit=5, window="1m")
        with pytest.raises(ValueError, match="no default rule"):
            policy.resolver()

    def test_token_bucket_rate_derived_from_window(self, backend):
        """capacity=60, window=1m → refill_rate = 60/60 = 1.0 token/sec"""
        policy  = RateLimitPolicy(backend).default(TokenBucket, capacity=60, window="1m")
        limiter = policy._routes["*"]
        assert isinstance(limiter, TokenBucket)
        assert abs(limiter.refill_rate - 1.0) < 1e-9

    def test_token_bucket_explicit_rate_overrides_window(self, backend):
        policy  = RateLimitPolicy(backend).default(TokenBucket, capacity=60, rate="2/s", window="1m")
        limiter = policy._routes["*"]
        assert abs(limiter.refill_rate - 2.0) < 1e-9

    def test_sliding_window_log_window_parsed(self, backend):
        policy  = RateLimitPolicy(backend).default(SlidingWindowLog, limit=10, window="30s")
        limiter = policy._routes["*"]
        assert isinstance(limiter, SlidingWindowLog)
        assert limiter.window_ms == 30_000
        assert limiter.limit     == 10

    def test_sliding_window_counter_window_parsed(self, backend):
        policy  = RateLimitPolicy(backend).default(SlidingWindowCounter, limit=10, window="2m")
        limiter = policy._routes["*"]
        assert isinstance(limiter, SlidingWindowCounter)
        assert limiter.window_ms == 120_000

    def test_token_bucket_requires_capacity(self, backend):
        with pytest.raises(ValueError, match="requires 'capacity'"):
            RateLimitPolicy(backend).default(TokenBucket, window="1m")

    def test_sliding_window_requires_limit(self, backend):
        with pytest.raises(ValueError, match="requires 'limit'"):
            RateLimitPolicy(backend).default(SlidingWindowLog, window="1m")

    def test_sliding_window_requires_window(self, backend):
        with pytest.raises(ValueError, match="requires 'window'"):
            RateLimitPolicy(backend).default(SlidingWindowLog, limit=10)

    def test_chaining_returns_RateLimitPolicy(self, backend):
        p = RateLimitPolicy(backend)
        assert p.limit("/a", SlidingWindowLog, limit=5, window="1m") is p
        assert p.default(TokenBucket, capacity=10, window="1m") is p

    def test_describe_returns_all_routes(self, backend):
        policy  = (
            RateLimitPolicy(backend)
            .limit("/api/login", SlidingWindowLog,     limit=5,  window="1m")
            .limit("/api/data",  SlidingWindowCounter, limit=10, window="1m")
            .default(TokenBucket, capacity=100, window="1m")
        )
        rows   = policy.describe()
        routes = {r["route"] for r in rows}
        assert routes == {"/api/login", "/api/data", "*"}


# ---------------------------------------------------------------------------
# End-to-end: RateLimitPolicy → resolver → middleware (via PythonBackend + TestClient)
# ---------------------------------------------------------------------------

def test_RateLimitPolicy_wired_into_middleware(backend):
    """Full path: RateLimitPolicy → resolver → middleware → route."""
    from starlette.testclient import TestClient
    from fastapi import FastAPI

    policy  = (
        RateLimitPolicy(backend)
        .limit("/api/login", SlidingWindowLog, limit=2, window="1m")
        .default(TokenBucket, capacity=10, window="1m")
    )

    from sluice import RateLimitMiddleware
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        resolver=policy.resolver(),
        exclude_paths={"/health"},
    )

    @app.get("/api/login")
    async def login(): return {"ok": True}

    @app.get("/api/data")
    async def data(): return {"ok": True}

    client = TestClient(app)

    # /api/login: limit=2
    assert client.get("/api/login").status_code == 200
    assert client.get("/api/login").status_code == 200
    assert client.get("/api/login").status_code == 429

    # /api/data: capacity=10 — unaffected by login exhaustion
    for _ in range(5):
        assert client.get("/api/data").status_code == 200
