# Sluice

**FastAPI-first asynchronous rate limiting for Python**, with Redis-backed atomic admission, three algorithms, and an explicit process-local degradation mode.

Sluice is designed around a single admission operation: each request is evaluated atomically by one backend. Redis Lua scripts use Redis server time, and weighted requests are admitted in full or rejected in full.

## Features

- Token Bucket, Sliding Window Log, and Sliding Window Counter algorithms.
- Redis-backed atomic admission using one Lua script per check.
- Weighted requests (`cost`) with all-or-nothing admission.
- FastAPI route dependencies as the primary integration; optional pure-ASGI middleware.
- Explicit Redis outage behavior through `HybridBackend` and bounded local fallback.
- Trusted-proxy-aware client identity helpers and HMAC-based identifier pseudonymization.
- Low-cardinality observer and in-process metrics interfaces.
- Python 3.11–3.13 support target.

## Installation

```bash
python -m pip install sluice-limiter
```

The package depends on FastAPI and `redis-py` (async API). Run Redis 7.4 or a compatible Redis server for distributed enforcement.

## FastAPI quick start

The route dependency is the recommended integration for new FastAPI applications. Create one backend at application startup, reuse it across requests, and close it during lifespan shutdown.

```python
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from sluice import RateLimitPolicy
from sluice.algorithms import SlidingWindowLog, TokenBucket
from sluice.backends import RedisBackend

backend = RedisBackend.from_url("redis://localhost:6379/0")

policy = (
    RateLimitPolicy(backend)
    .limit("/login", SlidingWindowLog, limit=5, window="1m")
    .default(TokenBucket, capacity=100, window="1m")
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await backend.connect()
    try:
        yield
    finally:
        await backend.close()

app = FastAPI(lifespan=lifespan)
router = APIRouter(dependencies=[Depends(policy.dependency())])

@router.post("/login")
async def login():
    return {"ok": True}

@router.get("/status")
async def status():
    return {"ok": True}

app.include_router(router)
```

## Redis connection settings

`RedisBackend.from_url()` forwards connection options to `redis-py`. Set a
finite pool size and socket timeouts for each application process:

```python
backend = RedisBackend.from_url(
    "redis://localhost:6379/0",
    max_connections=32,
    socket_connect_timeout=1.0,
    socket_timeout=1.0,
)
```

Choose `max_connections` for the expected concurrent Redis operations per
process and multiply by the number of worker processes when sizing Redis.
Sluice does not override `redis-py`'s pool or socket defaults; in the
redis-py 7.4.1 version inspected for this release, an unspecified
`max_connections` becomes `2**31` and socket timeouts are unset. Pool exhaustion
raises from Redis and is handled by `HybridBackend` according to its selected
failure policy. `HybridBackend.redis_call_timeout_sec` separately bounds
admissions and its `ping()` health check; `connect()` script initialization
uses the configured Redis socket timeouts.

`RateLimitPolicy.dependency()` snapshots the configured route map when called. Add all rules, including `.default(...)`, before creating the dependency. Route rules use FastAPI's resolved route template (for example `/users/{user_id}`).

Allowed responses include `RateLimit-Limit`, `RateLimit-Remaining`, and `RateLimit-Reset`. Rejected requests return HTTP 429 with `Retry-After`. Backend errors return HTTP 503 by default; `policy.dependency(fail_open=True)` explicitly opts into fail-open behavior.

## Algorithms

| Algorithm | State per key | Semantics | Good fit |
|---|---:|---|---|
| Token Bucket | O(1) | Smooth refill with bursts up to capacity | General API traffic and burst tolerance |
| Sliding Window Log | O(N) | Exact count over the rolling window | Login, payment, or other strict quotas |
| Sliding Window Counter | O(1) | Weighted approximation using adjacent buckets | High-cardinality or high-throughput APIs where approximation is acceptable |

Example direct use:

```python
from sluice.algorithms import SlidingWindowLog

limiter = SlidingWindowLog(backend, limit=20, window_ms=60_000)
result = await limiter.check("account:123", cost=3)

if result.allowed:
    ...
```

All algorithms expose `await limiter.check(identifier, cost=1)`. `cost` must be a positive integer, and weighted admission is all-or-nothing. Quotas and windows are bounded so intermediate Redis Lua arithmetic stays within its exact integer range; token-bucket refill rates must also keep retry/reset durations representable in milliseconds.

## Redis outage behavior

For applications that prefer local enforcement over total loss of service during a Redis outage, use `HybridBackend`:

```python
from sluice.backends import HybridBackend

backend = HybridBackend(
    redis_url="redis://localhost:6379/0",
    failure_threshold=5,
    recovery_timeout_sec=30,
    redis_call_timeout_sec=0.5,
    failure_policy="local_fallback",  # or "fail_closed"
)
```

The circuit breaker routes a complete admission operation to Redis or the local backend. With `failure_policy="local_fallback"`, any failed Redis call immediately falls back locally for that operation; the breaker opens only after `failure_threshold` consecutive failures, so later calls keep trying Redis until then. Set `failure_policy="fail_closed"` to raise `BackendUnavailableError` instead of making a local admission. The ASGI middleware and FastAPI dependency also offer an application-boundary fail-open option that skips enforcement on backend errors.

A timeout is ambiguous: Redis may have committed the operation before its response was lost, and local fallback cannot deduplicate it. This is best-effort availability, not exactly-once failover. During local degradation, each process enforces an independent quota: with `N` workers/instances, the effective aggregate allowance can approach `N × configured_limit`. Local state is lost on restart and bounded-cache eviction can reset a key's quota early.

If a strict global quota is mandatory, use `failure_policy="fail_closed"` with `HybridBackend` or configure the middleware/dependency to fail closed on Redis backend errors.

## Identity and proxy trust

By default, Sluice does not trust `X-Forwarded-For` from arbitrary peers. Configure `TrustedProxyConfig` with the exact proxy IPs/CIDRs before using forwarding headers. For authenticated quotas, use `authenticated_subject_identifier` only after your authentication layer has validated credentials and populated the authenticated principal. `jwt_subject_identifier` is retained as a compatibility alias; it does not decode or validate bearer tokens.

`hashed_identifier(identifier, secret=..., namespace=...)` creates an HMAC-SHA256 identifier. Use a secret of at least 16 bytes and keep it outside source control. HMAC pseudonymization is not a replacement for access controls or secret management.

## Optional ASGI middleware

For app-wide enforcement or applications that cannot use route dependencies, `RateLimitMiddleware` accepts a resolver or a single limiter:

```python
app.add_middleware(
    RateLimitMiddleware,
    resolver=policy.resolver(),
    failure_mode="closed",  # or "open", chosen deliberately
)
```

FastAPI route dependencies are preferred when per-route policy selection is needed. Middleware is pure ASGI and defaults to fail-closed (HTTP 503) on evaluation errors.

## Observability

Sluice exposes `RateLimitObserver`, `NoOpObserver`, `MetricsCollector`, and `InstrumentedBackend`. These are dependency-free integration points; export metrics through your application's existing telemetry stack. The built-in collector intentionally avoids labels containing user identifiers, raw keys, or request paths. See [observability design](docs/design/10-observability-and-operational-controls.md).

## Documentation

- [Algorithm selection and trade-offs](docs/tradeoffs/01-algorithm-selection.md)
- [Clock-skew considerations](docs/tradeoffs/02-clock-skew.md)
- [Migration guide](docs/migration-guide.md)
- [Development and verification](docs/development.md)
- [Release checklist](docs/release-checklist.md)
- [Architecture roadmap](docs/roadmap/README.md)

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -m "not redis_integration and not slow"
```

Run all unit, Redis integration, and fault-injection tests with one command: `python -m pytest -q`. The integration tests create and clean up disposable Docker services; see [development docs](docs/development.md) for prerequisites.

From a repository checkout, run the local algorithm benchmark:

```bash
python -m benchmarks.main --duration 30 --json benchmark-results.json
```

The benchmark uses deterministic synthetic timestamps and the in-process `FallbackBackend`. Its latency/throughput figures are **not Redis performance numbers**. For optional charts, install `.[bench]` and pass `--charts ./charts`.

## License

MIT. See [LICENSE](LICENSE).
