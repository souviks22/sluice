# Step 7 — Local backend and degradation semantics

## Local admission guarantees

`FallbackBackend.check()` now evaluates each `RateLimitOperation` directly under
one process-wide re-entrant lock. It uses one local timestamp per operation and
returns a `RateLimitResult` only after the full transition completes.

- Token bucket: deducts the requested cost only when the complete cost is
  available.
- Sliding-window log: prunes expired entries, checks `count + cost`, then adds
  all cost units or none.
- Sliding-window counter: computes the weighted prior/current bucket usage and
  increments the current count by the full cost or not at all.

The old `now_ms()` and `evalsha()` methods remain as compatibility methods. The
new algorithms use `check()` and do not route a logical admission through those
legacy calls.

## Bounded state and trade-offs

Each algorithm cache has a configurable `maxsize` and idle `ttl`. Invalid,
non-positive, or non-finite bounds are rejected at construction. LRU eviction
bounds memory when key cardinality exceeds the configured maximum; eviction can
reset a key's quota early. TTL expiry also discards idle state. These are
intentional availability/memory trade-offs, not equivalent to Redis persistence.

## Hybrid failure behavior

`HybridBackend` chooses a backend once for the complete `check()` operation.
Its `failure_policy` is `"local_fallback"` by default for compatibility;
`"fail_closed"` raises `BackendUnavailableError` after Redis errors/timeouts
and while the breaker is open. With local fallback selected, errors and
per-call timeouts route that operation to local state. After
`failure_threshold` consecutive Redis failures the breaker opens and subsequent
operations use local state until a half-open probe succeeds. A local backend
failure propagates to the caller (fail closed); it is not silently treated as
an allowed request.

A Redis timeout has an unavoidable ambiguous-outcome window: Redis may have
committed an admission even if the response was lost. Falling back locally can
therefore mean that one logical HTTP request has affected both stores. The
fallback is a best-effort availability policy, not exactly-once failover.

## Scope of local guarantees

Atomicity is process-local. With `N` application processes, a degraded service
can admit up to approximately `N` independent local quotas. Local state is not
shared across workers or hosts and is discarded on process restart. Operators
must choose whether that availability trade-off is appropriate for each
policy; deployments requiring a strict global quota should use fail-closed
behavior instead of local fallback.

## Verification

The new focused tests cover all-or-nothing weighted admission for all three
algorithms and validation of cache bounds. Full existing backend tests still
require the project's Redis/fakeredis test dependencies.
