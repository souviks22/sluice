# Step 2 — Baseline and compatibility map

**Status:** Static source inventory complete; runtime test baseline blocked by missing dependencies. **Scope:** Inventory only; no runtime package behavior or public API changed.

## 1. Repository baseline

The supplied archive contains 54 entries. Maintained areas:

| Area | Contents | Role |
|---|---|---|
| `sluice/algorithms/` | Base protocol; token bucket; sliding-window log; sliding-window counter | Admission algorithms |
| `sluice/backends/` | Backend protocol; Redis; in-memory fallback; hybrid/circuit breaker | State execution and degradation |
| `sluice/scripts/` | Three Lua scripts | Redis-side state transitions |
| `sluice/middleware/` | FastAPI middleware; policy builder; request utilities | HTTP integration and policy resolution |
| `tests/` | Six test modules | Algorithm, concurrency, backend, and policy tests |
| `examples/` | `basic_app.py` | FastAPI integration example |
| `benchmarks/` | Traffic patterns, runner, report, entrypoint | Performance experiments |
| `docs/design/` | Three design notes | Existing architecture rationale |
| `docs/tradeoffs/` | Algorithm and clock-skew notes | Existing trade-off rationale |

No CI workflow, lockfile, containerized Redis integration environment, migration guide, changelog, or dedicated ASGI/route-dependency integration test module is present in this snapshot.

## 2. Public API inventory and disposition

Dispositions are target decisions, not immediate removals. Preserve compatibility through a deprecation window where practical.

| Current surface | Location | Disposition | Target |
|---|---|---|---|
| `RateLimiter` | `sluice.algorithms.base` | IMPROVE | Stable limiter contract; validate inputs and define cost semantics |
| `RateLimitResult` | `sluice.algorithms.base` | KEEP / IMPROVE | Stable result model with explicit reset/retry semantics |
| `TokenBucket` | `sluice.algorithms.token_bucket` | KEEP / IMPROVE | Atomic operation; validate capacity/refill/cost |
| `SlidingWindowLog` | `sluice.algorithms.sliding_window_log` | KEEP / IMPROVE | Atomic weighted admission; exact semantics and bounded-storage policy |
| `SlidingWindowCounter` | `sluice.algorithms.sliding_window_counter` | KEEP / IMPROVE | Atomic weighted admission; document approximation |
| `RateLimitBackend` | `sluice.backends.base` | DEPRECATE / REPLACE | Operation-oriented atomic contract; hide script names and clock calls |
| `RedisBackend` | `sluice.backends.redis_backend` | KEEP / REWORK | Redis adapter, server-side time, script lifecycle, cluster-safe keys |
| `FallbackBackend` | `sluice.backends.fallback_backend` | KEEP AS OPTIONAL / REWORK | Explicit per-process semantics, bounded admission, eviction observability |
| `CircuitBreaker` | `sluice.backends.hybrid_backend` | KEEP / REWORK | Explicit state machine, half-open probe policy, deterministic tests |
| `HybridBackend` | `sluice.backends.hybrid_backend` | DEPRECATE / REPLACE | Policy-driven fail-open/fail-closed/local-degraded execution |
| `RateLimitMiddleware` | `sluice.middleware.fastapi` | DEPRECATE AS PRIMARY / KEEP OPTIONAL | Optional app-wide ASGI enforcement; dependencies are primary FastAPI API |
| `RateLimitPolicy` | `sluice.middleware.policy` | KEEP / REWORK | Immutable validated policy registry and route-aware matching |
| `build_resolver` / `LimitResolver` | `sluice.middleware.utils` | DEPRECATE / ADAPT | Replace literal path lookup with route-aware policy resolution |
| `ip_identifier` | `sluice.middleware.utils` | KEEP / HARDEN | Trusted-proxy-aware client address extraction |
| `ip_route_identifier` | `sluice.middleware.utils` | KEEP / REWORK | Stable route identity rather than raw URL path by default |
| `jwt_subject_identifier` | `sluice.middleware.utils` | DEPRECATE | Do not decode unverified JWTs; use authenticated principal integration |
| `parse_window` / `parse_rate` | `sluice.middleware.utils` | KEEP / HARDEN | Strict finite-positive parsing and overflow checks |
| Root exports | `sluice/__init__.py` | KEEP / REWORK | Ergonomic FastAPI-first exports; avoid eager optional adapter imports |
| Lua script filenames | `sluice/scripts/*.lua` | INTERNAL | Versioned internal implementation, not public API |

### Compatibility policy

1. Keep existing algorithm names and the `RateLimitResult` concept.
2. Preserve fluent policy configuration where it maps cleanly to an immutable policy model.
3. Keep middleware available for global enforcement, but make route dependencies the preferred FastAPI integration.
4. Add deprecation warnings before removing the old backend protocol, resolver functions, or unsafe JWT helper.
5. Do not promise compatibility for undocumented constructor arguments or internal Lua argument layouts.
6. A major-version boundary is appropriate for the backend contract and FastAPI integration redesign.

## 3. Current behavioral contract discovered

### Algorithm/backend boundary

The baseline archive used separate `now_ms()` and `evalsha()` calls and did
not make weighted sliding-window admission atomic. The current implementation
uses `RateLimitOperation` and `RateLimitBackend.check()` for one complete
admission. Redis runs one server-timed Lua transition; `FallbackBackend`
evaluates it under a process-local lock. `now_ms()` and `evalsha()` remain only
as compatibility methods. See the current [backend execution contract](../design/05-backend-execution-contract.md).

### FastAPI integration

The baseline archive used Starlette `BaseHTTPMiddleware` and failed open broadly.
The current `RateLimitMiddleware` is pure ASGI, defaults to fail-closed with
HTTP 503, and exposes an explicit fail-open option. It supports exact path and
descendant exclusions, emits rate-limit headers on normal and rejected
responses, and allows rejection response customization. Middleware exclusions
use the ASGI path; route-specific identity in middleware may therefore use
that raw path when route metadata is not available. The default IP helper
ignores forwarding headers unless the immediate peer is a configured trusted
proxy.

### Policy behavior

The baseline policy mutated limiter prefixes during resolver construction and
matched literal paths. The current `RateLimitPolicy` assigns an isolated key
namespace as each rule is built, then creates an immutable route-map snapshot
when `resolver()` or `dependency()` is called. Both match FastAPI's resolved
route template, such as `/users/{user_id}`. Changes to the builder after
snapshot creation do not alter an existing resolver or dependency.

### Redis and fallback behavior

The baseline archive caught broad exceptions from `SCRIPT LOAD`; the current
`RedisBackend.connect()` falls back to raw `EVAL` only when Redis explicitly
reports `SCRIPT` as unknown or unsupported. It stages script caches and
publishes them only after all scripts load. Other Redis errors propagate.
`HybridBackend.check()` routes one complete operation. Under
`failure_policy="local_fallback"`, local quotas are bounded and process-local;
they do not preserve fleet-wide enforcement, and a Redis timeout can have an
ambiguous commit outcome. See the current [Redis fallback contract](../design/02-redis-fallback.md).

## 4. Test inventory

| Test module | Current focus | Gaps to address |
|---|---|---|
| `test_algorithms.py` | Basic allow/reject, refill/window behavior, labels, key isolation | Cost > 1, invalid config, deterministic boundaries, reset/retry contracts |
| `test_concurrency.py` | Concurrent admission for each algorithm | Weighted-cost atomicity, multi-process Redis, cross-algorithm invariants |
| `test_circuit_breaker.py` | Circuit state transitions | Half-open concurrency, generation races, deterministic clock/jitter |
| `test_fallback.py` | Local backend behavior and memory estimates | Cache saturation/eviction abuse, per-key bounds, lock contention |
| `test_hybrid.py` | Redis/local routing and degradation | Failure between logical operation phases, fail modes, state consistency |
| `test_policy.py` | Parsing, fluent policy construction, middleware wiring | Parameterized routes, repeated resolver build, immutable policy, proxy trust |
| Missing real Redis integration | — | Redis version matrix, restart/script flush, cluster slot behavior, TTL cleanup |
| Missing ASGI integration | — | Lifespan, dependency integration, streaming/websocket behavior, exception paths |

## 5. Baseline verification

Environment observed: Python 3.13; pytest and pytest-asyncio present; FastAPI and Starlette present; `redis` Python package absent; `fakeredis` absent; `httpx` and `cachetools` present.

Command: `python -m pytest -q`

**Result: blocked during collection.** Six test modules fail to import because `redis` and/or `fakeredis` are missing. No test body ran. This is an environment/dependency failure, not a behavioral test result.

Packaging observations:

- `pyproject.toml` declares `sluice-limiter` version `0.1.3`.
- `sluice/__init__.py` exposes `__version__ = "0.1.1"`.
- Redis, cachetools, FastAPI, and Starlette are mandatory dependencies today.
- The development extra declares `httpx2`; verify its intent against the actual test-client stack before changing it.
- No lockfile or supported dependency matrix is present.

## 6. Transformation decisions

1. **FastAPI-first API:** route dependencies and an explicit limiter registry are the primary integration. Middleware remains optional for global coarse-grained protection.
2. **Route identity:** resolve policies against FastAPI route metadata/templates, not only raw request paths.
3. **Operation-oriented backend:** one call performs a complete check-and-consume operation, including authoritative time and atomic cost handling.
4. **Explicit failure policy:** distinguish fail-open, fail-closed, and local-degraded behavior; do not silently fail open for all exceptions.
5. **Identity boundary:** use authenticated principals for user/API-key limits and trusted-proxy-aware client IP resolution for IP limits.
6. **Immutable configuration:** policy registration must not mutate limiter instances or generate keys as a side effect of resolver creation.
7. **Optional dependencies:** importing the core API should not require importing Redis-specific modules; Redis adapter selection should have clear dependency behavior.
8. **Test pyramid:** deterministic unit tests, fake Redis tests where useful, real Redis integration, ASGI integration, and multi-process correctness tests.
9. **Observability:** stable decision metadata and metrics for allowed, rejected, degraded, backend errors, and local cache pressure.
10. **Migration discipline:** compatibility shims and deprecation notes for existing public surfaces.

## 7. Exit criteria

- [x] Source tree and package areas inventoried.
- [x] Existing public names catalogued.
- [x] KEEP / IMPROVE / DEPRECATE dispositions recorded.
- [x] Behavioral contracts and integration boundaries documented.
- [x] Test modules and missing coverage mapped.
- [x] Baseline test command attempted and blocker recorded.
- [ ] Runtime baseline green — deferred until Step 3 provisions declared test dependencies and Redis test services.

## 8. Next step

**Step 3 — Reproducible engineering environment and baseline harness.** Establish supported Python/FastAPI/Starlette/Redis versions, correct dependency metadata, add a reproducible local Redis test service, CI checks, and deterministic test fixtures. Do not redesign runtime behavior in Step 3.
