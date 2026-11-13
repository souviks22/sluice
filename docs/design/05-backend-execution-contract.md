# Backend execution contract

## Supported operation boundary

Algorithms now construct an immutable `RateLimitOperation` containing the
algorithm discriminator, fully namespaced key, immutable algorithm config, and
weighted request cost. The supported `RateLimitBackend` protocol exposes one
method:

```python
result = await backend.check(operation)
```

The returned `RateLimitResult` is the complete admission decision. Backend
resource lifecycle is a separate optional protocol (`BackendLifecycle`), and
the old `now_ms` / `evalsha` surface is retained as
`LegacyRateLimitBackend` for migration compatibility.

## Backend routing invariant

`HybridBackend.check()` makes exactly one circuit-breaker routing decision for
the complete operation. It does not route time acquisition and script execution
independently, preventing a single logical decision from starting on Redis and
finishing on the local backend.

## Admission and degradation guarantees

The Redis implementation performs one server-timed Lua transition per
operation. Weighted admission is all-or-nothing for all three Redis
algorithms. `FallbackBackend.check()` also evaluates a complete weighted
operation atomically under its process-local lock; the supported `check()`
path does not compose an admission from separate `now_ms()` and `evalsha()`
calls.

`HybridBackend` makes one backend selection for each complete operation. With
`failure_policy="local_fallback"`, a Redis error or timeout sends that
operation to this process's fallback backend. Each process maintains separate,
bounded, ephemeral state, so this mode does not preserve a fleet-wide quota.
If Redis times out after committing, the fallback cannot know that and the
operation may affect both stores. Hybrid fallback therefore provides neither
global enforcement during degradation nor exactly-once admission. A timeout
can also produce an ambiguous result for the caller if the fallback itself
fails after Redis may have committed.

With `failure_policy="fail_closed"`, Redis errors, timeouts, and an open
circuit raise `BackendUnavailableError`; the operation is not retried against
the local backend. This preserves the configured shared-backend enforcement
policy at the cost of rejecting requests while Redis is unavailable. The
middleware's `fail_open` mode is an application-boundary choice and skips
enforcement when backend evaluation fails.

The circuit breaker records backend errors and timeouts, opens after the
configured consecutive-failure threshold, and probes Redis after its recovery
delay. Its state controls routing; it does not improve the consistency of
local fallback. See [Redis fallback](02-redis-fallback.md) for the full state
machine and timeout behavior.

## Compatibility

The old protocol remains importable as `LegacyRateLimitBackend`; concrete
backends continue exposing `now_ms()` and `evalsha()` for migration
compatibility. New algorithm implementations depend only on
`RateLimitBackend.check()`. The legacy methods do not carry the atomicity
guarantees of a complete `check()` operation when composed by callers.
