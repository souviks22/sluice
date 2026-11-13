# Sluice V2 — Production Architecture Contract

**Status:** Proposed baseline for the V2 rebuild  
**Scope:** FastAPI-first rate limiting library with Redis-backed distributed enforcement  
**Rule:** Implement one migration step at a time; do not mix unrelated refactors.

## 1. Product objective

Sluice should make correct, observable, secure rate limiting the easiest path for a FastAPI application. Its primary job is admission control at the application boundary—not to be a generic distributed state framework.

The default production path must provide a clear and testable guarantee: for a given policy key, each admission decision is atomic across all application workers that share the Redis deployment. Any weaker behavior (for example, process-local degradation) must be explicitly selected and observable.

## 2. Product principles

1. **Correctness before throughput.** No optimization may weaken admission semantics silently.
2. **FastAPI-native first.** Public APIs should integrate naturally with FastAPI dependencies, routers, lifespan, request state, and exception handling.
3. **Redis is the distributed source of truth.** Do not silently replace cluster-wide enforcement with per-process counters.
4. **One decision, one atomic transition.** A check with `cost > 1` either consumes the full cost or consumes nothing.
5. **Fail behavior is policy.** `fail_open`, `fail_closed`, and local degradation are distinct explicit choices, not broad exception handlers.
6. **Identity is security-sensitive.** Trust authenticated principals and explicitly trusted proxy configuration; never trust arbitrary forwarding headers or unverified JWT claims by default.
7. **Stable public contracts.** Results, headers, policy matching, and lifecycle behavior are documented and tested.
8. **Operational visibility is part of correctness.** Degraded mode, backend errors, cache pressure, and rejections must be measurable without high-cardinality labels.
9. **No hidden mutation.** Building or reusing policies must not mutate limiter configuration or change Redis key namespaces.
10. **Test real semantics.** Fake Redis is useful for fast tests; real Redis integration tests are required for Lua, expiration, script loading, and cluster behavior.

## 3. Non-goals for the first production release

- A general-purpose distributed lock, quota ledger, or billing system.
- Automatic discovery of trusted reverse proxies.
- Silent local fallback advertised as globally consistent.
- A large plugin system or arbitrary user-supplied Lua execution.
- Supporting every ASGI framework equally; FastAPI is the primary integration target.

## 4. Required correctness invariants

### Admission and algorithms

- `cost` is a positive integer and is validated before backend execution.
- For each algorithm, `allowed == true` means exactly `cost` units were committed.
- For each algorithm, `allowed == false` means no units from that attempt were committed.
- The admission check and state mutation are one atomic operation.
- Redis-backed time is read inside the same Lua execution that evaluates and mutates state.
- Time moving backwards must not create extra quota. Algorithms must clamp or otherwise handle negative elapsed time safely.
- `remaining`, `reset_after`, and `retry_after` have documented semantics and deterministic boundary behavior.
- State keys have bounded cardinality through identifier normalization/limits and appropriate TTLs.

### Distributed backend

- All Redis keys touched by a script are declared in `KEYS`; scripts do not synthesize undeclared key names.
- Keys participating in one Redis Cluster script share a hash slot.
- Script cache misses and Redis `NOSCRIPT` recovery are handled without masking authentication, connection, or server errors.
- Backend startup, readiness, timeout, close, and ownership semantics are explicit.
- A backend error cannot be mistaken for an ordinary rate-limit rejection or allowance.

### FastAPI integration

- Route-specific policies use the resolved FastAPI route identity/template, not an unsafe raw-path guess.
- Application-wide policy and route-specific policy precedence is deterministic and documented.
- Identity extraction runs after authentication when the identity is an authenticated user/API client.
- Proxy-derived client IPs are trusted only when the immediate peer is in configured trusted proxy ranges.
- Excluded routes use explicit exact/pattern semantics; `/health` must not accidentally exclude `/health-admin`.
- Rejected responses include consistent rate-limit metadata and `Retry-After` where meaningful.
- Middleware/dependency failures follow the configured failure mode and are observable.

## 5. Target architecture

Keep the public surface small and separate policy, decision, storage, and framework integration.

```text
FastAPI application
  ├── Sluice lifespan integration
  │     └── backend lifecycle / readiness / shutdown
  ├── Global ASGI guard (optional, app-wide coarse policy)
  └── FastAPI route dependency / decorator (preferred route-aware enforcement)
          ├── identity resolver (authenticated principal / API key / trusted IP)
          ├── immutable policy (algorithm + limits + key scope + failure mode)
          └── RateLimiter service
                └── atomic backend operation
                      ├── Redis implementation (Lua, server time)
                      └── Local implementation (explicit single-process mode only)
```

### Package boundaries (target)

- `sluice/core/`: immutable configuration, result models, validation, key derivation, exceptions.
- `sluice/algorithms/`: algorithm definitions and pure parameter/semantics; no FastAPI imports.
- `sluice/backends/`: atomic backend protocol and Redis/local implementations.
- `sluice/fastapi/`: dependency factories, route integration, optional ASGI middleware, exception/response helpers, lifespan integration.
- `sluice/observability/`: metrics hooks and event types; no mandatory metrics vendor dependency.
- `sluice/scripts/`: versioned Lua scripts and script metadata.

The existing `sluice.middleware` import path may be retained temporarily as a compatibility shim during migration, then deprecated deliberately.

## 6. FastAPI integration model

The primary API should be route-aware and explicit, for example:

```python
from fastapi import Depends, FastAPI
from sluice import Sluice, RateLimit

sluice = Sluice.from_redis_url("redis://localhost:6379/0")
app = FastAPI(lifespan=sluice.lifespan)

@app.post("/login", dependencies=[Depends(sluice.limit("login", requests=5, window="1m"))])
async def login():
    ...
```

This is an API sketch, not an implemented contract yet. The implementation must ensure route resolution and authenticated identity are available at the enforcement point. A pure ASGI middleware cannot reliably inspect FastAPI's resolved route before routing; therefore it should be reserved for global/raw-scope policies, while route-specific enforcement should run through a FastAPI dependency or a route-handler integration.

A global app-wide guard may be composed with route limits, but precedence and whether both counters are charged must be explicit. The default should avoid surprising double charging.

## 7. Backend contract direction

Replace the split `now_ms()` + `evalsha()` protocol with a single semantic operation. The backend must own the complete atomic transition, including clock acquisition.

Conceptual interface:

```python
class RateLimitBackend(Protocol):
    async def consume(self, command: RateLimitCommand) -> RateLimitResult: ...
```

`RateLimitCommand` should be a validated immutable value containing algorithm, namespaced key, cost, and algorithm parameters. Redis translates it to one versioned Lua script invocation. Local mode implements the same contract under a lock. The backend must not expose raw script names or argument arrays to policy/application code.

## 8. Initial algorithm portfolio

Keep the three existing algorithms, but define their contracts precisely:

- **Token bucket:** burst control and sustained refill; O(1) state.
- **Sliding-window counter:** efficient general API default with explicitly approximate semantics.
- **Sliding-window log:** exact rolling-window semantics for low-volume/high-value endpoints, with explicit memory/cardinality cost.

Do not add fixed-window or leaky-bucket variants until the current three pass the invariant suite. Algorithm selection guidance must describe boundary behavior, cost support, memory growth, and retry/reset semantics.

## 9. Failure modes

Expose a typed failure policy, at minimum:

- `fail_open`: continue the request when the limiter backend is unavailable; emit an event/metric.
- `fail_closed`: reject with a service-unavailable response (not a 429, because the limit was not evaluated).
- `local`: explicitly switch to process-local enforcement; mark results as degraded and document that limits multiply across workers/instances.

Local mode is not globally consistent. It must not be the default production fallback. A circuit breaker may control backend retry behavior, but it must not silently change the selected failure policy.

## 10. HTTP contract

- Rejections use HTTP 429 and a stable machine-readable error body.
- Backend-unavailable rejection uses HTTP 503 under `fail_closed`.
- Emit the standardized `RateLimit-Limit`, `RateLimit-Remaining`, and `RateLimit-Reset` fields where their values are meaningful and consistent; include `Retry-After` for rejected requests when a retry estimate exists.
- Keep any legacy `X-RateLimit-*` headers behind a compatibility option during migration.
- Header values must be derived from the same atomic decision returned by the backend.

## 11. Verification strategy

1. Pure unit tests for validation, key derivation, policy precedence, and result semantics.
2. Deterministic algorithm tests using a controllable clock for the local backend.
3. Lua contract tests against fakeredis where supported.
4. Required integration suite against supported real Redis versions.
5. Redis Cluster tests for declared keys/hash-slot behavior if cluster support is claimed.
6. ASGI/FastAPI tests for route templates, dependency ordering, auth identity, proxy trust, exclusions, headers, and failure modes.
7. Multi-worker tests proving shared Redis limits and explicitly demonstrating local-mode multiplication.
8. Property-based and adversarial tests for cost, time boundaries, concurrency, identifier cardinality, and cache pressure.
9. Benchmarks only after correctness tests pass; report latency percentiles and throughput separately for Redis and local mode.

## 12. Step-by-step transformation plan

Each step ends with a reviewable artifact and a green, scoped test suite before the next step begins.

| Step | Deliverable | Exit criterion |
|---|---|---|
| 1. Architecture contract | This document: product scope, invariants, target boundaries, API direction, migration sequence | Decisions are explicit; no implementation changes mixed in |
| 2. Baseline and compatibility map | Inventory of public imports, tests, packaging, behavior, known defects | Every public API and behavior is classified as keep/change/deprecate |
| 3. Core domain model | Immutable config/command/result/error types and validation | Invalid inputs rejected before backend calls; compatibility tests defined |
| 4. Atomic backend contract | Single `consume()` semantic operation and lifecycle protocol | No external `now_ms()`; fake/local/Redis backends share contract |
| 5. Redis correctness | Server time in Lua, atomic cost, declared keys, TTLs, script recovery | Invariant suite passes against real Redis; cluster claim tested or explicitly excluded |
| 6. Algorithms | Corrected token bucket, sliding counter, sliding log | All algorithms pass boundary, cost, concurrency, and retry semantics |
| 7. FastAPI integration | Dependency-first route API, lifespan, global ASGI guard where appropriate | Route templates, auth ordering, middleware behavior tested end-to-end |
| 8. Identity and policy engine | Trusted identity providers, immutable route policies, precedence | No spoofable default identity path; deterministic matching |
| 9. Failure and resilience | Explicit modes, circuit breaker, bounded local backend | Degradation is visible and never misrepresented as distributed |
| 10. HTTP and observability | Headers, response schema, metrics hooks, structured events | Stable client contract and low-cardinality telemetry |
| 11. Production packaging/docs | Extras, supported versions, examples, migration guide, security notes | Clean install, type checks, docs examples, CI matrix |
| 12. Performance and release hardening | Load tests, profiling, soak tests, release checklist | Performance claims backed by reproducible results; release candidate accepted |

## 13. Step 1 decisions to carry forward

- FastAPI route dependencies are the primary route-aware integration; optional pure ASGI middleware is for global policy only.
- Redis-backed atomic consumption is the production default.
- Local fallback is explicit and never described as globally consistent.
- The backend API becomes one semantic atomic `consume()` call.
- `cost` is all-or-nothing for every algorithm.
- Redis time is read inside Lua.
- Policy/configuration objects are immutable after construction.
- Existing public APIs are inventoried before compatibility decisions are made.

These are the constraints for Step 2. No source-code behavior is changed by this document alone.
