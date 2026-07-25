# sluice

Distributed rate limiter for Python: Token Bucket, Sliding Window Log, and Sliding Window Counter — Redis-backed, Lua-atomic, with a FastAPI/Starlette middleware and a circuit-breaker fallback for Redis outages.

## Features

- **Three algorithms**, each with a documented memory/accuracy/clock-skew trade-off — pick per-route based on what the endpoint actually needs
- **Atomic by construction** — every check runs as a single Lua script (`EVALSHA`, cached via `SCRIPT LOAD` at connect time) so there's no check-then-act race between concurrent requests
- **Redis-authoritative time** — scripts use the Redis server clock (`TIME`) instead of client clocks, so limiter correctness doesn't depend on NTP sync across app nodes
- **Circuit breaker with local fallback** — a three-state breaker (`CLOSED` / `OPEN` / `HALF_OPEN`) that, on sustained Redis failures, degrades to a bounded, self-expiring in-process limiter (`TTLCache`) instead of failing open or failing closed
- **Declarative FastAPI middleware** — a fluent `RateLimitPolicy` builder maps routes to algorithms; per-route Redis keys are auto-namespaced so two routes never share a counter by accident
- **Pluggable backend** — `RateLimitBackend` is a `Protocol` (`now_ms` + `evalsha`); `RedisBackend`, `FallbackBackend`, and `HybridBackend` all implement it interchangeably

## Installation

```bash
pip install sluice
```

Requires Python 3.11+.

## Quick start

```python
from fastapi import FastAPI
from sluice import RateLimitMiddleware, RateLimitPolicy
from sluice.backends import RedisBackend
from sluice.algorithms import SlidingWindowCounter, SlidingWindowLog, TokenBucket

backend = RedisBackend.from_url("redis://localhost:6379/0")

policy = (
    RateLimitPolicy(backend)
    # Auth: exact counting — a ±5% error on a brute-force target is a
    # security issue, not a trade-off.
    .limit("/api/login",  SlidingWindowLog,     limit=5,  window="1m")
    # Expensive endpoint: approximate is fine, memory matters at scale.
    .limit("/api/report", SlidingWindowCounter, limit=10, window="1m")
    # Everything else: token bucket for smooth throughput, allows bursts.
    .default(TokenBucket, capacity=100, window="1m")
)

app = FastAPI()

@app.on_event("startup")
async def startup():
    await backend.connect()

app.add_middleware(RateLimitMiddleware, resolver=policy.resolver())
```

Rejected requests get a `429` with `RateLimit-*` headers and a `Retry-After`; allowed requests get the same headers attached so clients can self-throttle proactively. See `examples/basic_app.py` for a complete app with lifespan-managed startup/shutdown.

## Algorithms

| Algorithm | Memory | Accuracy | Clock-skew sensitivity | Best for |
|---|---|---|---|---|
| Token Bucket | O(1) — 2 fields/key | Allows controlled bursts | Benign (skew shifts refill slightly, self-corrects) | General-purpose APIs, smooth throughput |
| Sliding Window Log | O(N) — 1 entry per request in-window | Exact | Sensitive — skew delays pruning, slightly under-counts | Security-sensitive limits (login, payment) where false positives are costly |
| Sliding Window Counter | O(1) — 2 counters/key | Approximate, bounded error (~<5% typical) | Moderate — divergence > `window_ms` between nodes can create phantom capacity | High-traffic endpoints where memory matters more than exactness |

All three are exposed as async classes (`TokenBucket`, `SlidingWindowLog`, `SlidingWindowCounter`) sharing one interface:

```python
result = await limiter.check(identifier, cost=1)
# result.allowed, result.remaining, result.reset_after_ms, result.retry_after_ms
```

`identifier` scopes the counter (typically `f"{ip}:{path}"`); `cost` lets a single call consume more than one unit (native support in Token Bucket, sequential admission for the window-based algorithms).

## Handling Redis outages

A naive rate limiter has two bad options when Redis goes down: fail open (let everything through, right when abuse is most likely) or fail closed (take the service down). `HybridBackend` wraps `RedisBackend` in a circuit breaker that instead falls back to a local, per-node in-memory limiter:

```python
from sluice.backends import HybridBackend

backend = HybridBackend(
    redis_url="redis://localhost:6379/0",
    failure_threshold=5,       # consecutive failures before opening the circuit
    recovery_timeout_sec=30.0, # base backoff before probing Redis again
    redis_call_timeout_sec=0.5,
)
```

While the circuit is `OPEN`, each node enforces limits independently — the effective cluster-wide limit becomes `N × configured_limit` for `N` running instances — which is a documented, deliberate trade-off rather than an oversight: legitimate traffic keeps flowing and each node still bounds abuse, and it self-corrects the moment Redis recovers (via a single `HALF_OPEN` probe, jittered so instances don't all re-probe in lockstep).

## Declarative policies

`RateLimitPolicy` keeps all rate-limit configuration in one place instead of scattering decorators across route handlers, and auto-namespaces Redis keys per route so two `TokenBucket` rules never collide on the same default key prefix:

```python
policy = (
    RateLimitPolicy(backend)
    .limit("/api/upload", TokenBucket, capacity=10, rate="5/min")
    .default(TokenBucket, capacity=100, window="1m")
    .identifier(jwt_subject_identifier)  # rate-limit per authenticated user instead of per IP
)
```

Built-in identifier functions: `ip_identifier`, `ip_route_identifier` (default), `jwt_subject_identifier`.

## Project layout

```
sluice/
├── algorithms/     token_bucket.py, sliding_window_log.py, sliding_window_counter.py
├── backends/        redis_backend.py, fallback_backend.py, hybrid_backend.py
├── middleware/      fastapi.py, policy.py, utils.py
└── scripts/         token_bucket.lua, sliding_window_log.lua, sliding_window_counter.lua
```

- `docs/design/` — write-ups on the atomic-Lua approach, the Redis-fallback design, and declarative rate limiting
- `docs/tradeoffs/` — algorithm selection guide and a deep dive on clock skew
- `benchmarks/` — traffic-pattern simulations (uniform, burst, window-boundary hammer, Poisson) comparing all three algorithms; run with `python -m benchmarks.main`
- `tests/` — algorithm correctness, circuit breaker state transitions, fallback behavior, hybrid backend integration, concurrency, and policy building

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests run against `fakeredis` by default, so no live Redis instance is required.

## License

MIT